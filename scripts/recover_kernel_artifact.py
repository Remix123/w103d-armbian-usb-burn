#!/usr/bin/env python3
"""Revalidate and normalize an already compiled Ophub artifact after a verifier-tool failure."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from prepare_kernel_input import verify_inner_checksums


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def copy_regular_debs(source_archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source_archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"unsafe path in deb archive: {member.name}")
            if member.isfile() and path.name.endswith(".deb"):
                stream = bundle.extractfile(member)
                if stream is None:
                    raise SystemExit(f"cannot read deb archive member {member.name}")
                destination = target / path.name
                destination.write_bytes(stream.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--source-run-id", required=True)
    ap.add_argument("--source-head-sha", required=True)
    ap.add_argument("--source-log", type=Path, required=True)
    ap.add_argument("--recovery-run-id", required=True)
    args = ap.parse_args()
    log = args.source_log.read_text(errors="replace")
    if "rg: command not found" not in log:
        raise SystemExit("source workflow failure log does not contain the reviewed missing-rg signature")

    lock_candidates = [p for p in args.artifact_dir.rglob("sources.lock.json")]
    lock_candidates += [p for p in args.artifact_dir.rglob("sources-lock-fallback.json")]
    if not lock_candidates:
        raise SystemExit("source artifact has no source lock")
    if len({sha(p) for p in lock_candidates}) != 1:
        raise SystemExit(f"source artifact contains conflicting source locks: {[str(p) for p in lock_candidates]}")
    lock = json.loads(lock_candidates[0].read_text())
    version = lock.get("kernel", {}).get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"invalid numeric kernel version in source lock: {version!r}")

    runtime_archives = [p for p in args.artifact_dir.rglob(f"{version}.tar.gz")]
    if len(runtime_archives) != 1:
        raise SystemExit(f"expected exactly one build output {version}.tar.gz; found {[str(p) for p in runtime_archives]}")
    runtime_archive = runtime_archives[0]
    checked_members = verify_inner_checksums(runtime_archive, version)

    deb_dir = args.artifact_dir / "normalized-debs"
    direct_debs = list(args.artifact_dir.rglob("*.deb"))
    if direct_debs:
        deb_dir.mkdir(parents=True, exist_ok=True)
        for source in direct_debs:
            shutil.copy2(source, deb_dir / source.name)
    else:
        deb_archives = list(args.artifact_dir.rglob(f"deb-{version}.tar.gz"))
        if len(deb_archives) != 1:
            raise SystemExit(f"expected one deb-{version}.tar.gz archive; found {len(deb_archives)}")
        copy_regular_debs(deb_archives[0], deb_dir)
    if not list(deb_dir.glob("*.deb")):
        raise SystemExit("kernel artifact has no .deb packages")

    normalized = args.output_dir / "validated-artifact"
    (normalized / "outputs").mkdir(parents=True, exist_ok=True)
    (normalized / "upstream/ophub/compile-kernel/output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(lock_candidates[0], normalized / "outputs/sources.lock.json")
    shutil.copy2(runtime_archive, normalized / f"upstream/ophub/compile-kernel/output/{version}.tar.gz")
    shutil.copytree(deb_dir, normalized / "outputs/kernel-debs", dirs_exist_ok=True)
    manifest_path = normalized / "outputs/kernel-packages.json"
    subprocess.run([
        "python3", "scripts/create_kernel_manifest.py", "--deb-dir", str(normalized / "outputs/kernel-debs"),
        "--version", version, "--out", str(manifest_path),
    ], check=True)

    output = args.output_dir / "recovered-kernel-input"
    subprocess.run([
        "python3", "scripts/prepare_kernel_input.py", "--artifact-dir", str(normalized),
        "--output-dir", str(output), "--run-id", args.source_run_id,
    ], check=True)
    provenance = {
        "recovery_run_id": str(args.recovery_run_id),
        "source_compile_run_id": str(args.source_run_id),
        "source_compile_head_sha": args.source_head_sha,
        "source_compile_workflow_path": ".github/workflows/compile-kernel.yml",
        "source_compile_step_conclusion": "success",
        "source_verification_step_conclusion": "failure",
        "source_failure_signature": "rg: command not found",
        "revalidation": "passed: source lock, exact numeric runtime archive, nested sha256sums, all kernel deb metadata",
        "source_runtime_archive": str(runtime_archive.relative_to(args.artifact_dir)),
        "source_runtime_archive_sha256": sha(runtime_archive),
        "source_lock_sha256": sha(lock_candidates[0]),
        "validated_inner_members": checked_members,
    }
    toolchain_versions = list(args.artifact_dir.rglob("outputs-toolchain-version.txt"))
    if len(toolchain_versions) == 1:
        shutil.copy2(toolchain_versions[0], output / "toolchain-version.txt")
    (output / "recovery-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps({k: provenance[k] for k in ("recovery_run_id", "source_compile_run_id", "source_compile_head_sha", "source_runtime_archive", "source_runtime_archive_sha256", "revalidation")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
