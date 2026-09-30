#!/usr/bin/env python3
"""Validate and extract the single approved reference IMG from its ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import zipfile


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text())
    zip_path = args.zip.resolve(strict=True)
    if sha256(zip_path) != lock["zip_sha256"]:
        raise SystemExit("reference ZIP SHA-256 mismatch")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        entries = [i for i in archive.infolist() if not i.is_dir()]
        if len(entries) != 1:
            raise SystemExit(f"expected exactly one file, found {len(entries)}")
        info = entries[0]
        member = PurePosixPath(info.filename)
        if member.is_absolute() or ".." in member.parts or info.filename != lock["member_name"]:
            raise SystemExit(f"unexpected ZIP member path: {info.filename!r}")
        if info.file_size != lock["image_size"]:
            raise SystemExit(f"IMG size mismatch: {info.file_size}")
        output = args.output_dir / lock["member_name"]
        if output.exists():
            raise SystemExit(f"refusing to overwrite {output}")
        digest = hashlib.sha256()
        written = 0
        with archive.open(info) as source, output.open("xb") as target:
            while chunk := source.read(8 * 1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        if written != lock["image_size"] or digest.hexdigest() != lock["image_sha256"]:
            output.unlink(missing_ok=True)
            raise SystemExit("reference IMG size or SHA-256 mismatch")
    print(json.dumps({"zip": str(zip_path), "zip_sha256": lock["zip_sha256"],
                      "image": str(output), "image_size": written,
                      "image_sha256": digest.hexdigest()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
