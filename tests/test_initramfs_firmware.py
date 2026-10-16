"""MT7663 rootfs and initramfs consistency, plus real Linux CPIO roundtrips."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import initramfs_firmware as firmware
from assemble_usb_image import install_wifi_firmware, ensure_uinitrd, raw_initrd
from verify_usb_image import verify_legacy_image

LOCK = json.loads((ROOT / 'config/wifi-firmware.lock.json').read_text())


def rootfs(base):
    root = base / 'rootfs'
    destination = root / LOCK['install_dir']
    destination.mkdir(parents=True)
    for entry in LOCK['files']:
        shutil.copyfile(ROOT / entry['repo_path'], destination / entry['name'])
    return root


class FirmwareTreeTests(unittest.TestCase):
    def test_real_repository_firmware_overrides_old_and_compressed(self):
        with tempfile.TemporaryDirectory() as td:
            root = rootfs(Path(td))
            target = root / LOCK['install_dir'] / LOCK['files'][0]['name']
            target.write_bytes(b'2020 firmware fixture')
            target.with_name(target.name+'.gz').write_bytes(gzip.compress(b'old'))
            install_wifi_firmware(root, LOCK)
            records = firmware.verify_tree(root, LOCK)
            self.assertEqual(len(records), 2)
            self.assertTrue(all(r['matches'] for r in records))

    def test_higher_priority_and_symlink_copies_are_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            root = rootfs(Path(td))
            high = root / 'usr/lib/firmware/updates/6.18.56-ophub/mediatek'
            high.mkdir(parents=True)
            (high / LOCK['files'][0]['name']).write_bytes(b'old')
            (high / LOCK['files'][1]['name']).symlink_to('/missing')
            with self.assertRaises(ValueError):firmware.verify_tree(root, LOCK)
            install_wifi_firmware(root, LOCK)
            self.assertEqual(len(firmware.verify_tree(root, LOCK)), 4)

    def test_missing_pair_and_changed_rootfs_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = rootfs(Path(td))
            (root / LOCK['install_dir'] / LOCK['files'][0]['name']).unlink()
            with self.assertRaises(ValueError):firmware.verify_tree(root, LOCK)

    def test_correct_compressed_firmware_is_compared_after_decompression(self):
        with tempfile.TemporaryDirectory() as td:
            root = rootfs(Path(td))
            path = root / LOCK['install_dir'] / LOCK['files'][0]['name']
            path.with_name(path.name+'.gz').write_bytes(gzip.compress(path.read_bytes()))
            self.assertEqual(len(firmware.verify_tree(root, LOCK)), 3)


LINUX_TOOLS = sys.platform == 'linux' and all(shutil.which(p) for p in ('cpio', 'unmkinitramfs', 'mkimage', 'find'))


@unittest.skipUnless(LINUX_TOOLS, 'requires native Linux GNU cpio, unmkinitramfs and mkimage')
class CpioRoundtripTests(unittest.TestCase):
    def make_payload(self, base, root, *, old=False, missing=False, early=False):
        layer = base / 'layers/main'
        layer.mkdir(parents=True)
        (layer / 'init').write_bytes(b'#!/bin/sh\necho retained-init\n')
        (layer / 'init').chmod(0o755)
        module = layer / 'usr/lib/modules/6.18.56-ophub/kernel/test.ko'
        module.parent.mkdir(parents=True)
        module.write_bytes(b'retain-module-build')
        os.link(module, module.with_name('hardlink.ko'))
        (layer / 'lib').symlink_to('usr/lib')
        if not missing:
            destination = layer / LOCK['install_dir']
            destination.mkdir(parents=True)
            for entry in LOCK['files']:
                (destination / entry['name']).write_bytes(b'2020 fixture' if old else (root / LOCK['install_dir'] / entry['name']).read_bytes())
            if old:
                (destination / (LOCK['files'][0]['name']+'.gz')).write_bytes(gzip.compress(b'2020 compressed fixture'))
        layers = []
        if early:
            e = base / 'layers/early';e.mkdir()
            (e / 'early-marker').write_bytes(b'keep early cpio')
            high = e / 'lib/firmware/updates/mediatek';high.mkdir(parents=True)
            (high / LOCK['files'][0]['name']).write_bytes(b'early old fixture')
            layers.append(e)
        layers.append(layer)
        payload = base / 'input-initrd.img'
        firmware.write_archive(layers, payload, base)
        return payload

    def test_old_main_and_early_firmware_rebuilt_and_wrapped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td);root = rootfs(base)
            payload = self.make_payload(base, root, old=True, early=True)
            original = payload.read_bytes()
            output, report = firmware.repair_initramfs(payload, root, LOCK, base/'repair', 'CONFIG_RD_GZIP=y\n')
            self.assertTrue(report['rebuilt'])
            self.assertEqual(payload.read_bytes(), original)
            self.assertEqual(list(report['after']), ['early','main'])
            self.assertTrue(all(r['matches'] for rows in report['after'].values() for r in rows))
            extracted = base/'repair/after'
            self.assertEqual((extracted/'early/early-marker').read_bytes(), b'keep early cpio')
            self.assertEqual((extracted/'main/init').read_bytes(), b'#!/bin/sh\necho retained-init\n')
            self.assertEqual(stat.S_IMODE((extracted/'main/init').stat().st_mode), 0o755)
            module = extracted/'main/usr/lib/modules/6.18.56-ophub/kernel/test.ko'
            self.assertEqual(module.read_bytes(), b'retain-module-build')
            self.assertEqual(module.stat().st_ino, module.with_name('hardlink.ko').stat().st_ino)
            self.assertTrue((extracted/'main/lib').is_symlink())
            boot = base/'boot';boot.mkdir()
            wrapped = boot/'uInitrd'
            ensure_uinitrd(boot, output, wrapped)
            self.assertEqual(verify_legacy_image(wrapped), output.read_bytes())
            self.assertEqual(raw_initrd(wrapped), output.read_bytes())
            self.assertEqual(firmware.digest(output), report['payload_sha256'])

    def test_missing_firmware_is_added_even_without_wifi_in_original_initrd(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=rootfs(base);payload=self.make_payload(base, root, missing=True)
            output,report=firmware.repair_initramfs(payload,root,LOCK,base/'repair','CONFIG_RD_GZIP=y')
            self.assertTrue(report['rebuilt'])
            self.assertEqual(len(report['missing_main_firmware']),2)
            self.assertEqual(len(report['after']['main']),2)

    def test_already_correct_archive_is_retained(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=rootfs(base);payload=self.make_payload(base, root)
            output,report=firmware.repair_initramfs(payload,root,LOCK,base/'repair','')
            self.assertFalse(report['rebuilt'])
            self.assertEqual(output,payload)
            self.assertEqual(report['source_payload_sha256'],report['payload_sha256'])

    def test_rebuild_requires_kernel_gzip_support(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=rootfs(base);payload=self.make_payload(base,root,old=True)
            with self.assertRaisesRegex(ValueError,'CONFIG_RD_GZIP'):
                firmware.repair_initramfs(payload,root,LOCK,base/'repair','')


if __name__ == '__main__':unittest.main()
