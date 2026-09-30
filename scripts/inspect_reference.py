#!/usr/bin/env python3
"""Create a compact, read-only layout report from a GitHub-unpacked Amlogic image."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
import gzip
import lzma
import bz2


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(*argv: str) -> str:
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
    return p.stdout.strip()


def sparse_to_raw(src: Path, dst: Path) -> dict:
    crc = 0
    with src.open("rb") as f:
        h = f.read(28)
        magic, major, minor, file_hdr, chunk_hdr, block_size, blocks, chunks, expected_crc = struct.unpack("<IHHHHIIII", h)
        if (magic, major, file_hdr, chunk_hdr) != (0xED26FF3A, 1, 28, 12):
            raise ValueError(f"unsupported sparse header in {src.name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("wb") as out:
            out.truncate(block_size * blocks)
            logical = 0
            for _ in range(chunks):
                ch = f.read(12)
                kind, reserved, count, total = struct.unpack("<HHII", ch)
                length = count * block_size
                if reserved:
                    raise ValueError("nonzero sparse reserved field")
                if kind == 0xCAC1:
                    if total != 12 + length:
                        raise ValueError("bad RAW chunk size")
                    left = length
                    while left:
                        data = f.read(min(left, 8 << 20))
                        if not data:
                            raise ValueError("truncated RAW chunk")
                        out.seek(logical)
                        out.write(data)
                        crc = zlib.crc32(data, crc)
                        logical += len(data)
                        left -= len(data)
                elif kind == 0xCAC2:
                    if total != 16:
                        raise ValueError("bad FILL chunk size")
                    pattern = f.read(4)
                    if len(pattern) != 4:
                        raise ValueError("truncated FILL chunk")
                    full, rem = divmod(length, 4)
                    fill = pattern * min(full, (8 << 20) // 4)
                    todo = length
                    out.seek(logical)
                    while todo:
                        n = min(todo, len(fill))
                        out.write(fill[:n])
                        crc = zlib.crc32(fill[:n], crc)
                        logical += n
                        todo -= n
                elif kind == 0xCAC3:
                    if total != 12:
                        raise ValueError("bad DONT_CARE chunk size")
                    # A sparse hole reads as zero; update the logical CRC in bounded chunks.
                    zero = bytes(min(length, 8 << 20))
                    todo = length
                    while todo:
                        n = min(todo, len(zero))
                        crc = zlib.crc32(zero[:n], crc)
                        todo -= n
                    logical += length
                elif kind == 0xCAC4:
                    if total != 16 or count != 0:
                        raise ValueError("bad CRC chunk")
                    chunk_crc = struct.unpack("<I", f.read(4))[0]
                    if chunk_crc != crc:
                        raise ValueError("sparse CRC chunk mismatch")
                else:
                    raise ValueError(f"unknown sparse chunk type {kind:#x}")
            if logical != blocks * block_size or (expected_crc and crc != expected_crc):
                raise ValueError("sparse image size or CRC mismatch")
    return {"raw_bytes": blocks * block_size, "block_size": block_size, "blocks": blocks,
            "chunks": chunks, "crc32": f"{crc:08x}", "sha256": digest(dst)}


def ext4_metadata(path: Path) -> dict | None:
    with path.open("rb") as f:
        f.seek(1024)
        sb = f.read(1024)
    if len(sb) < 1024 or struct.unpack_from("<H", sb, 56)[0] != 0xEF53:
        return None
    blocks = struct.unpack_from("<I", sb, 4)[0]
    if struct.unpack_from("<I", sb, 96)[0] & 0x80:
        blocks |= struct.unpack_from("<I", sb, 336)[0] << 32
    block_size = 1024 << struct.unpack_from("<I", sb, 24)[0]
    return {"label": sb[120:136].split(b"\0")[0].decode("ascii", "replace"),
            "uuid": "-".join((sb[104:108].hex(), sb[108:110].hex(), sb[110:112].hex(), sb[112:114].hex(), sb[114:120].hex())),
            "blocks": blocks, "block_size": block_size, "filesystem_bytes": blocks * block_size}


def fdt_scan(path: Path, scratch: Path) -> list[dict]:
    data = path.read_bytes()
    magic = b"\xd0\x0d\xfe\xed"
    found = []
    pos = 0
    while True:
        pos = data.find(magic, pos)
        if pos < 0:
            break
        if pos + 8 <= len(data):
            total = struct.unpack_from(">I", data, pos + 4)[0]
            if 40 <= total <= len(data) - pos:
                blob = scratch / f"{path.name}.fdt-{pos:x}.dtb"
                blob.write_bytes(data[pos:pos + total])
                item = {"offset": pos, "size": total, "sha256": digest(blob)}
                try:
                    dts = run("dtc", "-I", "dtb", "-O", "dts", str(blob))
                    lines = dts.splitlines()
                    item["model_and_compatible"] = [x.strip() for x in lines if re.search(r'\b(model|compatible)\s*=', x)][:30]
                    item["partition_related_lines"] = [x.strip() for x in lines if re.search(r'partition|label|reg\s*=', x, re.I)][:120]
                    item["dtc_parsed"] = True
                except (OSError, subprocess.CalledProcessError) as exc:
                    item["dtc_parsed"] = False
                    item["dtc_error"] = str(exc)[:500]
                found.append(item)
        pos += 4
    return found


def cpio_newc_summary(data: bytes) -> dict | None:
    pos = 0
    names = []
    selected = {}
    while pos + 110 <= len(data) and data[pos:pos + 6] in (b"070701", b"070702"):
        try:
            fields = [int(data[pos + 6 + i * 8:pos + 14 + i * 8], 16) for i in range(13)]
            size, namesize = fields[6], fields[11]
            start_name = pos + 110
            name = data[start_name:start_name + namesize - 1].decode("utf-8", "replace")
            pos = (start_name + namesize + 3) & ~3
            body = data[pos:pos + size]
            if len(body) != size:
                return None
            pos = (pos + size + 3) & ~3
            if name == "TRAILER!!!":
                break
            names.append(name)
            if name.lstrip("./") in ("init", "init.rc", "sbin/bootup", "sbin/boot", "recovery.fstab") or name.endswith(".rc") or "/init.d/" in name:
                selected[name] = body[:8192].decode("utf-8", "replace")
        except (ValueError, IndexError):
            return None
    if not names:
        return None
    return {"entry_count": len(names), "paths": names[:250], "selected_boot_files": selected}


def unpack_android_boot(path: Path, scratch: Path) -> dict:
    with path.open("rb") as f:
        head = f.read(64)
    info = {"file_type": run("file", "-b", str(path)), "magic_hex": head[:8].hex()}
    if not head.startswith(b"ANDROID!"):
        return info
    info["android_boot_magic"] = True
    cfg, kernel, ramdisk = scratch / "boot.cfg", scratch / "boot.kernel", scratch / "boot.ramdisk"
    try:
        out = run("abootimg", "-x", str(path), str(cfg), str(kernel), str(ramdisk))
        info["abootimg"] = out[:6000]
        if cfg.exists():
            info["header_config"] = cfg.read_text(errors="replace")[:6000]
        if kernel.exists():
            info["kernel"] = {"size": kernel.stat().st_size, "sha256": digest(kernel), "file_type": run("file", "-b", str(kernel))}
        if ramdisk.exists():
            info["ramdisk"] = {"size": ramdisk.stat().st_size, "sha256": digest(ramdisk), "file_type": run("file", "-b", str(ramdisk))}
            payload = ramdisk.read_bytes()
            try:
                if payload.startswith(b"\x1f\x8b"):
                    payload = gzip.decompress(payload)
                elif payload.startswith(b"\xfd7zXZ\x00"):
                    payload = lzma.decompress(payload)
                elif payload.startswith(b"BZh"):
                    payload = bz2.decompress(payload)
                elif payload[:4] == b"\x02!\x21\x4c":
                    decoded = subprocess.run(["lz4", "-dc", str(ramdisk)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
                    payload = decoded
                cpio = cpio_newc_summary(payload)
                if cpio:
                    info["bootstrap_initramfs"] = cpio
                else:
                    info["bootstrap_initramfs"] = {"archive_detected": False, "payload_prefix_hex": payload[:32].hex()}
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                info["initramfs_decode_error"] = str(exc)[:500]
    except (OSError, subprocess.CalledProcessError) as exc:
        info["abootimg_error"] = str(exc)[:800]
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unpacked", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--legacy-lock", type=Path, required=True)
    args = ap.parse_args()
    root = args.unpacked.resolve(strict=True)
    report: dict = {"unpacked_directory": str(root), "components": [], "bootfs": {}, "rootfs": {}}
    legacy = json.loads(args.legacy_lock.read_text())
    component_hashes = {}
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        item = {"name": p.name, "size": p.stat().st_size, "sha256": digest(p)}
        component_hashes[p.name] = item["sha256"]
        if p.name.endswith(".PARTITION") or p.name == "DDR.USB":
            with p.open("rb") as f:
                item["first_16_bytes_hex"] = f.read(16).hex()
            item["file_type"] = run("file", "-b", str(p))
        report["components"].append(item)
    cfg = root / "image.cfg"
    if not cfg.is_file():
        raise SystemExit("Amlogic packer did not extract image.cfg")
    report["image_cfg"] = cfg.read_text(errors="replace")
    report["legacy_reference_comparison"] = {
        "historical_reference_img_sha256": legacy["historical_reference_img_sha256"],
        "partition_components": {name: {"matches_old": component_hashes.get(name) == old_hash,
                                         "current_sha256": component_hashes.get(name), "historical_sha256": old_hash}
                                 for name, old_hash in legacy["partition_sha256"].items()},
        "all_partition_components_match_old": all(component_hashes.get(name) == old_hash for name, old_hash in legacy["partition_sha256"].items()),
        "system_boot_files": {}
    }
    with tempfile.TemporaryDirectory(prefix="w103d-ref-") as td:
        tmp = Path(td)
        for name, key in (("system.PARTITION", "bootfs"), ("data.PARTITION", "rootfs")):
            src = root / name
            if not src.is_file():
                report[key]["present"] = False
                continue
            with src.open("rb") as f:
                sparse_magic = struct.unpack("<I", f.read(4))[0]
            if sparse_magic == 0xED26FF3A:
                raw = tmp / f"{key}.raw"
                report[key]["sparse"] = sparse_to_raw(src, raw)
            else:
                raw = src
                report[key]["sparse"] = None
            report[key]["raw_size"] = raw.stat().st_size
            if key == "rootfs":
                report[key]["ext4"] = ext4_metadata(raw)
                if shutil.which("debugfs") and report[key]["ext4"]:
                    listing = run("debugfs", "-R", "ls -l /", str(raw))
                    report[key]["root_directory_entries"] = [x.strip() for x in listing.splitlines() if x.strip()][:80]
            else:
                paths = run("mdir", "-i", str(raw), "-s", "::")
                boot_listing = [line.rstrip() for line in paths.splitlines() if line.strip()]
                report[key]["fat_listing"] = boot_listing[:160]
                with tempfile.TemporaryDirectory(prefix="w103d-fat-") as fatdir:
                    dest = Path(fatdir)
                    run("mcopy", "-s", "-i", str(raw), "::*", str(dest) + "/")
                    files = [x for x in dest.rglob("*") if x.is_file()]
                    report[key]["files"] = [{"path": x.relative_to(dest).as_posix(), "size": x.stat().st_size,
                                             "sha256": digest(x)} for x in sorted(files)]
                    selected = ("u-boot.ext", "bootup.bmp", "emmc_autoscript", "emmc_autoscript.cmd", "uEnv.txt", "boot.scr", "uInitrd")
                    report[key]["boot_chain"] = {}
                    for filename in selected:
                        found = next((x for x in files if x.name == filename), None)
                        if not found:
                            continue
                        entry = {"path": found.relative_to(dest).as_posix(), "size": found.stat().st_size,
                                 "sha256": digest(found)}
                        if filename.endswith(".cmd") or filename == "uEnv.txt":
                            entry["text"] = found.read_text(errors="replace")[:12000]
                        report[key]["boot_chain"][filename] = entry
                    old_boot = legacy["system_boot_files_sha256"]
                    report["legacy_reference_comparison"]["system_boot_files"] = {
                        name: {"matches_old": report[key]["boot_chain"].get(name, {}).get("sha256") == old_hash,
                               "current_sha256": report[key]["boot_chain"].get(name, {}).get("sha256"),
                               "historical_sha256": old_hash}
                        for name, old_hash in old_boot.items()
                    }
        for name in ("_aml_dtb.PARTITION", "boot.PARTITION", "recovery.PARTITION"):
            part = root / name
            if not part.is_file():
                continue
            if name == "_aml_dtb.PARTITION":
                report.setdefault("vendor_dtb", {})[name] = {
                    "size": part.stat().st_size, "sha256": digest(part),
                    "fdt_blobs": fdt_scan(part, tmp),
                    "note": "FDT regions are found by the standard big-endian FDT magic; opaque wrapper bytes remain represented by the full partition hash."
                }
            else:
                scratch = tmp / name
                scratch.mkdir(exist_ok=True)
                report.setdefault("bootstrap_partitions", {})[name] = unpack_android_boot(part, scratch)
    if report["legacy_reference_comparison"]["all_partition_components_match_old"]:
        report["legacy_reference_comparison"]["layout_reuse_note"] = "All vendor partition components are byte-identical to the previously reviewed reference; prior vendor-layout findings may be reused."
    else:
        report["legacy_reference_comparison"]["layout_reuse_note"] = "One or more vendor partitions differ. Only current cloud inspection results may be used for this image."
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"component_count": len(report["components"]),
                      "boot_chain_files": sorted(report.get("bootfs", {}).get("boot_chain", {})),
                      "bootfs_raw_size": report.get("bootfs", {}).get("raw_size"),
                      "rootfs_raw_size": report.get("rootfs", {}).get("raw_size"),
                      "report": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
