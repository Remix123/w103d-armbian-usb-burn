"""Regression checks for the observed Actions handoff failures; no kernel build."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import create_kernel_manifest as inventory
import driver_variant as driver
import kernel_cache
import kernel_patch
import orchestrate_build as orchestrator
import prepare_kernel_input as staging
import publish_release


def lock_for(variant):
    config=driver.CONFIGS[variant]
    return {'ophub': {'commit':'a'*40}, 'kernel': {'repository':'ophub/linux-6.18.y','version':'6.18.56',
            'commit':'b'*40, 'config_sha256':'c'*64}, 'kernel_toolchain':{'sha256':'d'*64},
            'container':{'digest':'sha256:'+'e'*64}, 'kernel_patch_config':config,
            'kernel_patches':kernel_patch.load_patches(ROOT / config), 'resolution_time_utc':'now'}


def fixture(root, variant='stock'):
    out=root/'outputs';out.mkdir(parents=True)
    lock=lock_for(variant)
    (out/'sources.lock.json').write_text(json.dumps(lock))
    debs=out/'kernel-debs';debs.mkdir()
    packages=[]
    for name in ('linux-image','linux-dtb-meson64'):
        f=debs/(name+'.deb');f.write_bytes(name.encode())
        packages.append({'file':f.name,'package':name,'version':'6.18.56-1-ophub','architecture':'arm64',
                         'size':f.stat().st_size,'sha256':staging.sha256(f)})
    archive=out/'kernel-tarball/6.18.56.tar.gz';archive.parent.mkdir()
    payloads={f'{prefix}6.18.56-ophub.tar.gz':b'fixture' for prefix in ('boot-','modules-','header-','dtb-amlogic-')}
    checks=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in payloads.items()).encode()
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in {**payloads,'sha256sums':checks}.items():
            item=tarfile.TarInfo('6.18.56/'+name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
    manifest={'kernel_release':'6.18.56-ophub','packages':packages,'runtime_archive':{
        'file':archive.name,'size':archive.stat().st_size,'sha256':staging.sha256(archive)}}
    (out/'kernel-packages.json').write_text(json.dumps(manifest))
    return out,lock


class DriverTests(unittest.TestCase):
    def test_empty_stock_lock_is_valid_but_missing_is_rejected(self):
        lock=lock_for('stock')
        self.assertEqual(driver.validate_driver_lock(lock),'stock')
        driver.validate_patch_provenance(lock,{'kernel_patches':[]})
        del lock['kernel_patches']
        with self.assertRaises(ValueError):driver.validate_driver_lock(lock)

    def test_stock_apply_and_verify_pass_but_v23_default_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td);subprocess.run(['git','init','-q',str(src)],check=True)
            lock=src/'lock.json';lock.write_text(json.dumps({'kernel':{'repository':'ophub/linux-6.18.y'}}))
            base=[sys.executable,str(ROOT/'scripts/kernel_patch.py')]
            for command in ('check','apply','verify-applied'):
                result=subprocess.run([*base,command,'--src',str(src),'--lock',str(lock),
                    '--config',driver.CONFIGS['stock']],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run([*base,'verify-applied','--src',str(src),'--lock',str(lock)],capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_reuse_rejects_other_driver_and_changed_patch(self):
        lock=lock_for('v23')
        with self.assertRaises(ValueError):driver.validate_driver_lock(lock,driver.CONFIGS['stock'])
        lock['kernel_patches'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):driver.validate_driver_lock(lock)

    def test_tar_only_feeds_do_not_use_deb_download_mode(self):
        for variant in ('stock','v23'):
            metadata=f"KERNEL_TAGS='stable_{variant}'\nDOWNLOAD_TYPE='deb'\nDISK_TYPE='emmc'\n"
            updated=driver.configure_tar_feed(metadata,lock_for(variant))
            self.assertIn("DOWNLOAD_TYPE='tar'",updated)
            self.assertIn("DISK_TYPE='emmc'",updated)
            with self.assertRaises(ValueError):driver.configure_tar_feed(metadata,lock_for('stock' if variant=='v23' else 'v23'))

    def test_release_tags_are_separate_and_repeat_builds_unique(self):
        img='W103D_Armbian_26.11.0_6.18.56_USB_Burning_Tool.img'
        a=publish_release.release_tag(img,'a'*40,'v23','1-a1')
        b=publish_release.release_tag(img,'a'*40,'stock','1-a1')
        self.assertNotEqual(a,b)
        self.assertNotEqual(a,publish_release.release_tag(img,'a'*40,'v23','1-a2'))


class ArtifactTests(unittest.TestCase):
    def test_cache_roundtrip_both_variants(self):
        for variant in ('v23','stock'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as td:
                out,lock=fixture(Path(td),variant)
                kernel_cache.verify_cache(lock,out)
                staging.prepare_input(out,Path(td)/'input','123')
                report=json.loads((Path(td)/'input/kernel-input-manifest.json').read_text())
                self.assertEqual(report['ophub_source_lock']['kernel_patch_config'],driver.CONFIGS[variant])

    def test_cache_missing_runtime_tarball_fails(self):
        with tempfile.TemporaryDirectory() as td:
            out,lock=fixture(Path(td));next((out/'kernel-tarball').glob('*')).unlink()
            with self.assertRaises(SystemExit):kernel_cache.verify_cache(lock,out)

    def test_cache_tampered_deb_or_tarball_fails(self):
        for child in ('kernel-debs/linux-image.deb','kernel-tarball/6.18.56.tar.gz'):
            with self.subTest(child=child),tempfile.TemporaryDirectory() as td:
                out,lock=fixture(Path(td));(out/child).write_bytes(b'changed')
                with self.assertRaises(SystemExit):kernel_cache.verify_cache(lock,out)

    def test_cache_identity_covers_build_inputs_but_not_clock(self):
        lock=lock_for('v23');key=kernel_cache.cache_key(lock)
        changed=copy.deepcopy(lock);changed['resolution_time_utc']='later'
        self.assertEqual(key,kernel_cache.cache_key(changed))
        for path in [('kernel','config_sha256'),('ophub','commit'),('kernel_toolchain','sha256'),('container','digest')]:
            changed=copy.deepcopy(lock);changed[path[0]][path[1]]='changed'
            self.assertNotEqual(key,kernel_cache.cache_key(changed))
        self.assertNotEqual(key,kernel_cache.cache_key(lock_for('stock')))

    def test_stale_cache_lock_fails(self):
        with tempfile.TemporaryDirectory() as td:
            out,lock=fixture(Path(td));other=copy.deepcopy(lock);other['kernel']['config_sha256']='different'
            with self.assertRaises(ValueError):kernel_cache.verify_cache(other,out)

    def test_manifest_named_and_legacy_cli(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as td:
                base=Path(td);debs=base/'debs';debs.mkdir()
                for name in ('linux-image','linux-dtb-meson64'):(debs/(name+'.deb')).write_bytes(b'fixture')
                def fake_run(args,**kwargs):
                    field=args[-1];name=Path(args[-2]).stem
                    value={'Package':name,'Version':'6.18.56-1-ophub','Architecture':'arm64'}[field]
                    return subprocess.CompletedProcess(args,0,stdout=value+'\n')
                args=['manifest',str(debs)] if legacy else ['manifest','--deb-dir',str(debs),'--version','6.18.56','--out',str(base/'kernel-packages.json')]
                with patch.object(sys,'argv',args),patch.dict(os.environ,{'KERNEL_VERSION':'6.18.56'}),patch.object(inventory.subprocess,'run',side_effect=fake_run):
                    self.assertEqual(inventory.main(),0)
                output=debs/'kernel-manifest.json' if legacy else base/'kernel-packages.json'
                doc=json.loads(output.read_text());self.assertEqual(doc['kernel_release'],'6.18.56-ophub')
                self.assertEqual(doc['packages'][0]['architecture'],'arm64')


    def test_manifest_real_ophub_package_names_and_wrong_family(self):
        for family in ('meson64', 'rockchip64'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as td:
                base=Path(td)
                for name in ('linux-image', 'linux-dtb-'+family):
                    (base/(name+'.deb')).write_bytes(b'fixture')
                def fake_run(args, **kwargs):
                    field=args[-1];name=Path(args[-2]).stem
                    value={'Package':name+'-ophub','Version':'6.18.56-1-ophub','Architecture':'arm64'}[field]
                    return subprocess.CompletedProcess(args,0,stdout=value+'\n')
                with patch.object(sys,'argv',['manifest','--deb-dir',str(base),'--version','6.18.56']),patch.object(inventory.subprocess,'run',side_effect=fake_run):
                    if family=='meson64':
                        self.assertEqual(inventory.main(),0)
                        self.assertIn('linux-dtb-meson64-ophub',json.loads((base/'kernel-manifest.json').read_text())['dtb_packages'])
                    else:
                        with self.assertRaises(SystemExit):inventory.main()


class WorkflowContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml
        cls.workflows={p.name:yaml.safe_load(p.read_text()) for p in (ROOT/'.github/workflows').glob('*.yml')}

    def test_dispatch_fields_match_child_contracts(self):
        inputs={'compile':{'run_kernel_build','patch_config','pipeline_id'},'system':{'kernel_run_id','patch_config','pipeline_id'},
                'usb':{'system_run_id','patch_config','pipeline_id'},'zip':{'usb_run_id','patch_config','pipeline_id'}}
        for stage,fields in inputs.items():
            workflow=self.workflows[orchestrator.WORKFLOWS[stage][0]]
            trigger=workflow.get('on',workflow.get(True))
            self.assertTrue(fields <= set(trigger['workflow_dispatch']['inputs']))

    def test_parent_entries_share_correlated_orchestrator(self):
        for name,config in [('build-and-deliver.yml',driver.CONFIGS['v23']),('build-usb-stock-driver.yml',driver.CONFIGS['stock'])]:
            w=self.workflows[name]
            self.assertEqual(w['permissions']['actions'],'write')
            self.assertEqual(w['concurrency']['group'],'w103d-full-source-pipeline')
            steps=w['jobs']['build-deliver']['steps']
            stage=next(s for s in steps if s.get('run')=='python3 scripts/orchestrate_build.py')
            self.assertEqual(stage['env']['PATCH_CONFIG'],config)

    def test_usb_upload_uses_the_verified_image_basename(self):
        steps=self.workflows['build-usb-burn-image.yml']['jobs']['assemble-verify']['steps']
        upload=next(s for s in steps if s.get('id')=='upload-image')
        self.assertEqual(upload['with']['name'],'${{ steps.verified-image.outputs.image_name }}')
        self.assertIs(upload['with']['archive'],False)

    def test_attempt_1_does_not_match_attempt_10(self):
        self.assertFalse(orchestrator.pipeline_matches('Compile (pipeline-123-10)','pipeline-123-1'))
        self.assertTrue(orchestrator.pipeline_matches('Compile (pipeline-123-1)','pipeline-123-1'))

    def test_dispatch_correlates_run_not_latest(self):
        good={'id':1,'event':'workflow_dispatch','head_branch':'main','head_sha':'a'*40,
              'path':'.github/workflows/compile-kernel.yml','display_title':'Compile (pipeline-123-1)'}
        bad={**good,'id':2,'display_title':'Compile (pipeline-999-1)'}
        found=orchestrator.select_correlated_run([bad,good],pipeline_id='pipeline-123-1',ref='main',head_sha='a'*40,workflow_path=good['path'])
        self.assertEqual(found['id'],1)


if __name__=='__main__':unittest.main()
