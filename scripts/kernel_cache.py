#!/usr/bin/env python3
"""Key and validate complete kernel output caches, including their runtime tarball."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import os
import tempfile

from driver_variant import validate_driver_lock
from prepare_kernel_input import prepare_input

ROOT = Path(__file__).resolve().parent.parent
# Include compiler orchestration/verification code, not unrelated firmware/USB files.
BUILD_FILES = [".github/workflows/compile-kernel.yml", "scripts/resolve_sources.py",
               "scripts/kernel_patch.py", "scripts/prepare_locked_recompile.py",
               "scripts/create_kernel_manifest.py", "scripts/prepare_kernel_input.py",
               "scripts/driver_variant.py", "scripts/kernel_cache.py"]


def identity(lock: dict) -> dict:
    validate_driver_lock(lock)
    return {key: lock[key] for key in ("ophub", "kernel", "kernel_toolchain", "container", "kernel_patches", "kernel_patch_config")}


def cache_key(lock: dict) -> str:
    payload = {"sources": identity(lock), "build_code": {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in BUILD_FILES}}
    return "w103d-kernel-v2-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verify_cache(lock: dict, outputs: Path) -> None:
    cached = json.loads((outputs / "sources.lock.json").read_text())
    if identity(cached) != identity(lock):
        raise ValueError("cached kernel sources/config/driver/toolchain differ from the current lock")
    manifest = json.loads((outputs / "kernel-packages.json").read_text())
    if not manifest.get("runtime_archive"):
        raise ValueError("cache lacks the runtime archive digest; rebuild this obsolete cache")
    with tempfile.TemporaryDirectory(prefix="w103d-cache-check-") as td:
        prepare_input(outputs, Path(td), "cache")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["key", "verify"])
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--outputs", type=Path, default=Path("outputs"))
    a = ap.parse_args()
    lock = json.loads(a.lock.read_text())
    if a.command == "key":
        key = cache_key(lock)
        print(key)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write("cache_key=" + key + "\n")
    else:
        verify_cache(lock, a.outputs)
        print("Complete kernel cache validated against the current source lock")


if __name__ == "__main__":
    main()
