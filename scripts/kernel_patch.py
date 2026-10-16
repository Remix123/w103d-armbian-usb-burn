#!/usr/bin/env python3
"""Check, apply and confirm the W103D kernel patch set against a kernel tree.

The patch set is fixed by config/kernel-patches.json (path + SHA-256).  Every
command fails loudly; nothing is skipped on a patch that does not apply, unlike
the Ophub recompile ``-p`` option, which only prints a warning.

  check           git apply --check on a (possibly sparse) checkout; records the
                  patch set in the source lock so later stages can verify it
  apply           verifies the lock entry, runs --check, then applies
  verify-applied  confirms every patch is still applied (reverse --check)
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_patches(config_path: Path) -> list[dict]:
    config = json.loads(config_path.read_text())
    patches = []
    for entry in config["patches"]:
        path = REPO_ROOT / entry["path"]
        if not path.is_file():
            raise SystemExit(f"kernel patch is missing: {entry['path']}")
        actual = sha256(path)
        if actual != entry["sha256"]:
            raise SystemExit(f"kernel patch {entry['path']} SHA-256 {actual} differs from config {entry['sha256']}")
        files = sorted(set(re.findall(r"^diff --git a/(\S+) b/", path.read_text(), re.M)))
        if not files:
            raise SystemExit(f"kernel patch {entry['path']} touches no files")
        patches.append({"path": entry["path"], "sha256": actual, "files": files,
                        "description": entry.get("description", "")})
    # Empty patches list is valid (stock kernel build)
    return patches


def git_apply(src: Path, patch: str, *extra: str) -> None:
    result = subprocess.run(["git", "-C", str(src), "apply", "--verbose", *extra, str(REPO_ROOT / patch)],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode:
        mode = " ".join(extra) or "apply"
        raise SystemExit(f"git apply {mode} failed for {patch} in {src} (rc={result.returncode})")


def lock_entries(lock_path: Path) -> list[dict]:
    lock = json.loads(lock_path.read_text())
    entries = lock.get("kernel_patches")
    if not isinstance(entries, list):
        raise SystemExit(f"source lock {lock_path} has no kernel_patches entry; run the check stage first")
    return entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("check", "apply", "verify-applied"))
    ap.add_argument("--src", type=Path, required=True, help="kernel source checkout (git work tree)")
    ap.add_argument("--lock", type=Path, required=True, help="sources.lock.json")
    ap.add_argument("--config", type=Path, default=REPO_ROOT / "config/kernel-patches.json",
                    help="patch config file (default: config/kernel-patches.json)")
    args = ap.parse_args()
    src = args.src.resolve(strict=True)
    config_path = args.config if args.config.is_absolute() else REPO_ROOT / args.config
    if not config_path.is_file():
        raise SystemExit(f"patch config not found: {config_path}")
    patches = load_patches(config_path)

    if args.command == "check":
        series = json.loads(config_path.read_text()).get("kernel_series")
        locked_repo = json.loads(args.lock.read_text())["kernel"]["repository"]
        if series and locked_repo != f"ophub/linux-{series}":
            raise SystemExit(f"Ophub now builds W103D from {locked_repo}, but the W103D patch set targets "
                             f"linux-{series}; port and re-test the patch before building")
        if patches:  # Skip patch application for empty (stock) config
            for patch in patches:
                git_apply(src, patch["path"], "--check")
        lock = json.loads(args.lock.read_text())
        lock["kernel_patches"] = patches
        lock["kernel_patch_config"] = str(config_path.relative_to(REPO_ROOT))
        args.lock.write_text(json.dumps(lock, indent=2) + "\n")
        print(json.dumps({"kernel_patches_check": "passed", "patches": patches}, indent=2))
        return 0

    locked = lock_entries(args.lock)
    if [(p["path"], p["sha256"]) for p in locked] != [(p["path"], p["sha256"]) for p in patches]:
        raise SystemExit(f"patch set in the repository differs from the source lock: {locked} vs {patches}")
    if args.command == "apply":
        if patches:
            for patch in patches:
                git_apply(src, patch["path"], "--check")
            for patch in patches:
                git_apply(src, patch["path"])
    if patches:
        for patch in patches:
            git_apply(src, patch["path"], "--reverse", "--check")
    changed = subprocess.run(["git", "-C", str(src), "diff", "--stat"], check=True, text=True,
                             stdout=subprocess.PIPE).stdout
    print(changed, end="")
    print(json.dumps({f"kernel_patches_{args.command.replace('-', '_')}": "passed",
                      "patches": [p["path"] + " " + p["sha256"] for p in patches]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
