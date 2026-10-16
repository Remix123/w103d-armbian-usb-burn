#!/usr/bin/env python3
"""Audit and rebuild Debian initramfs archives with the locked MT7663 firmware.

Uses native unmkinitramfs/GNU cpio: no ARM chroot, module install or boot hooks.
Early CPIO layers remain uncompressed and ordered; the main layer uses gzip.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import zlib

SUFFIXES = ('', '.xz', '.gz', '.zst')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def firmware_paths(tree: Path, name: str) -> list[Path]:
    """Include updates/version subdirectories; never follow directory symlinks."""
    names = {name + suffix for suffix in SUFFIXES}
    result = []
    for directory, _, files in os.walk(tree, followlinks=False):
        parent = Path(directory)
        if 'firmware' in parent.relative_to(tree).parts:
            result.extend(parent / f for f in files if f in names)
    return sorted(result)


def inspect_tree(tree: Path, lock: dict, *, require_pair: bool = True) -> list[dict]:
    records = []
    for entry in lock['files']:
        paths = firmware_paths(tree, entry['name'])
        if require_pair and not paths:
            raise ValueError(f"initramfs/rootfs lacks {entry['name']}: {tree}")
        for path in paths:
            if path.is_symlink() or not path.is_file():
                records.append({'path': path.relative_to(tree).as_posix(), 'name': entry['name'],
                                'sha256': None, 'matches': False, 'reason': 'not a regular firmware file'})
                continue
            try:
                data = path.read_bytes()
                if path.suffix == '.xz':data = lzma.decompress(data)
                elif path.suffix == '.gz':data = gzip.decompress(data)
                elif path.suffix == '.zst':
                    data = subprocess.run(['zstd', '-q', '-d', '-c', str(path)], check=True, stdout=subprocess.PIPE).stdout
                actual = hashlib.sha256(data).hexdigest()
            except (OSError, EOFError, lzma.LZMAError, zlib.error, subprocess.CalledProcessError):
                actual = None
            records.append({'path': path.relative_to(tree).as_posix(), 'name': entry['name'],
                            'sha256': actual, 'matches': actual == entry['sha256']})
    return records


def verify_tree(tree: Path, lock: dict, *, require_pair: bool = True) -> list[dict]:
    records = inspect_tree(tree, lock, require_pair=require_pair)
    bad = [r for r in records if not r['matches']]
    if bad:
        raise ValueError(f"MT7663 firmware differs from the source lock in {tree}: {bad}")
    return records


def replace_copies(tree: Path, rootfs: Path, lock: dict, *, install_pair: bool) -> None:
    for entry in lock['files']:
        source = rootfs / lock['install_dir'] / entry['name']
        if source.is_symlink() or digest(source) != entry['sha256']:
            raise ValueError(f"rootfs firmware must be locked before rebuilding initramfs: {source}")
        content = source.read_bytes()
        paths = firmware_paths(tree, entry['name'])
        if install_pair:
            paths.append(tree / lock['install_dir'] / entry['name'])
        # Collapse compressed copies to the same plain name, including updates/*.
        targets = set()
        for path in paths:
            plain = path if path.name == entry['name'] else path.with_name(entry['name'])
            if not plain.parent.resolve().is_relative_to(tree.resolve()):
                raise ValueError(f"firmware directory escapes the extracted initramfs: {plain}")
            path.unlink(missing_ok=True)
            targets.add(plain)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)


def archive_layers(unpacked: Path) -> list[Path]:
    # unmkinitramfs writes a single archive directly into the destination;
    # early+main archives instead get named subdirectories. Normalize the
    # scratch tree only, so the same inspection works for both formats.
    if (unpacked / 'init').exists() or (unpacked / 'init').is_symlink():
        entries = list(unpacked.iterdir())
        temporary = unpacked / '.normalize-main'
        temporary.mkdir(exist_ok=False)
        for entry in entries:
            entry.rename(temporary / entry.name)
        temporary.rename(unpacked / 'main')
    names = {p.name for p in unpacked.iterdir()}
    if 'main' not in names or any(not re.fullmatch(r'main|early(?:[2-9]|[1-9][0-9]+)?', n) for n in names):
        raise ValueError(f"unexpected unmkinitramfs layout: {sorted(names)}")
    early = sorted((p for p in unpacked.iterdir() if p.name != 'main'),
                   key=lambda p: int(p.name[5:] or '1'))
    layers = [*early, unpacked / 'main']
    if any(p.is_symlink() or not p.is_dir() for p in layers):
        raise ValueError('unmkinitramfs layers must be actual directories')
    return layers


def unpack(payload: Path, target: Path) -> list[Path]:
    if target.exists():
        raise ValueError(f'use a fresh initramfs scratch directory: {target}')
    subprocess.run(['unmkinitramfs', str(payload.resolve()), str(target.resolve())], check=True)
    return archive_layers(target)


def write_archive(layers: list[Path], output: Path, scratch: Path) -> None:
    """GNU cpio preserves modes, symlinks, hardlinks and device nodes."""
    with output.open('xb') as destination:
        for index, layer in enumerate(layers):
            paths = subprocess.run(['find', '.', '-print0'], cwd=layer, check=True, stdout=subprocess.PIPE).stdout
            listing = b'\0'.join(sorted(paths.rstrip(b'\0').split(b'\0'))) + b'\0'
            archive = scratch / f'layer-{index}.cpio'
            with archive.open('xb') as out:
                subprocess.run(['cpio', '--null', '-o', '--format=newc', '--reproducible', '--quiet'],
                               cwd=layer, input=listing, stdout=out, check=True)
            with archive.open('rb') as inp:
                if layer.name == 'main':
                    with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
                        shutil.copyfileobj(inp, compressed)
                else:
                    shutil.copyfileobj(inp, destination)
            archive.unlink()


def repair_initramfs(payload: Path, rootfs: Path, lock: dict, scratch: Path, kernel_config: str) -> tuple[Path, dict]:
    """Return a checked raw initrd payload; rebuild stale OR missing firmware."""
    scratch.mkdir(parents=True, exist_ok=False)
    root_records = verify_tree(rootfs, lock)
    layers = unpack(payload, scratch / 'before')
    before = {p.name: inspect_tree(p, lock, require_pair=False) for p in layers}
    main_names = {r['name'] for r in before['main']}
    missing = sorted({e['name'] for e in lock['files']} - main_names)
    changed = bool(missing or any(not r['matches'] for rows in before.values() for r in rows))
    result = payload
    if changed:
        if 'CONFIG_RD_GZIP=y' not in kernel_config.splitlines():
            raise ValueError('rebuilding this initramfs requires CONFIG_RD_GZIP=y')
        for layer in layers:
            replace_copies(layer, rootfs, lock, install_pair=layer.name == 'main')
        result = scratch / 'repaired-initrd.img'
        write_archive(layers, result, scratch)
    # Always extract again, including when the existing payload was retained.
    final_layers = unpack(result, scratch / 'after')
    checked = {p.name: verify_tree(p, lock, require_pair=p.name == 'main') for p in final_layers}
    report = {'rebuilt': changed, 'source_payload_sha256': digest(payload), 'payload_sha256': digest(result),
              'missing_main_firmware': missing, 'before': before, 'after': checked,
              'rootfs': root_records, 'rebuild_method': 'early-newc-plus-gzip-main' if changed else 'unchanged'}
    (scratch / 'firmware-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return result, report
