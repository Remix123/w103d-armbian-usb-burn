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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unpacked", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    root = args.unpacked.resolve(strict=True)
    report: dict = {"unpacked_directory": str(root), "components": [], "bootfs": {}, "rootfs": {}}
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        item = {"name": p.name, "size": p.stat().st_size, "sha256": digest(p)}
        if p.name.endswith(".PARTITION") or p.name == "DDR.USB":
            with p.open("rb") as f:
                item["first_16_bytes_hex"] = f.read(16).hex()
            item["file_type"] = run("file", "-b", str(p))
        report["components"].append(item)
    cfg = root / "image.cfg"
    if not cfg.is_file():
        raise SystemExit("Amlogic packer did not extract image.cfg")
    report["image_cfg"] = cfg.read_text(errors="replace")
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
