#!/usr/bin/env python3
"""Build the locked, unmodified W103D MT76 driver modules against recovered headers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_kernel_input import verify_inner_checksums


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def one(paths: list[Path], what: str) -> Path:
    if len(paths) != 1:
        raise RuntimeError(f"expected one {what}; found {len(paths)}")
    return paths[0]


def checked_extract(archive: Path, out: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            p = PurePosixPath(member.name)
            if p.is_absolute() or ".." in p.parts:
                raise RuntimeError(f"unsafe path in locked headers archive: {member.name}")
        # Python's data filter rejects escaping symlinks and special files.
        tf.extractall(out, filter="data")


def run_logged(argv: list[str], log: Path, timeout: int, cwd: Path | None = None) -> None:
    start = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        log.write_text((partial + f"\ncommand timeout after {timeout}s\n")[-200_000:])
        raise RuntimeError(f"command timed out after {timeout}s; see {log.name}") from exc
    text = p.stdout or ""
    log.write_text(text[-200_000:])
    if p.returncode:
        raise RuntimeError(f"command failed rc={p.returncode}: {' '.join(argv[:5])}; see {log.name}")
    print(json.dumps({"command": argv[:5], "seconds": round(time.monotonic()-start, 2),
                      "log": log.name}, separators=(",", ":")), flush=True)


def modinfo(path: Path, field: str) -> str | None:
    try:
        p = subprocess.run(["modinfo", "-F", field, str(path)], text=True,
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--audit-report", type=Path, required=True)
    ap.add_argument("--recovery-dir", type=Path, required=True)
    ap.add_argument("--source-dir", type=Path, required=True)
    ap.add_argument("--toolchain-archive", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.lock = args.lock.resolve()
    args.audit_report = args.audit_report.resolve()
    args.recovery_dir = args.recovery_dir.resolve()
    args.source_dir = args.source_dir.resolve()
    args.toolchain_archive = args.toolchain_archive.resolve()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {"build_kind": "unmodified-baseline", "modules": [], "errors": [],
                    "limitations": ["This is an isolated GHA build; no module is installed or loaded on a device."]}
    try:
        lock = json.loads(args.lock.read_text())
        fixed = lock["kernel_input_manifest"]
        audit = json.loads(args.audit_report.read_text())
        if (not audit.get("audit_complete") or not audit.get("inputs_ready")
                or not audit.get("metadata_validation", {}).get("ok")
                or not audit.get("artifact_provenance_valid")):
            raise RuntimeError("static recovery artifact audit is incomplete or inputs_ready is false")
        expected_commit = lock["kernel_input_manifest"]["ophub_source_lock"]["kernel"]["commit"]
        p = subprocess.run(["git", "-C", str(args.source_dir), "rev-parse", "HEAD"],
                           text=True, capture_output=True, timeout=15, check=True)
        if p.stdout.strip() != expected_commit:
            raise RuntimeError("source checkout does not match locked Ophub kernel commit")
        report["source"] = {"repository": "ophub/linux-6.18.y", "commit": expected_commit,
                            "working_tree_clean": subprocess.run(
                                ["git", "-C", str(args.source_dir), "status", "--porcelain"],
                                text=True, capture_output=True, timeout=15, check=True).stdout.strip() == ""}
        source_lock = lock["kernel_input_manifest"]["ophub_source_lock"]
        report["provenance"] = {
            "recovery_run_id": lock["kernel_input_manifest"]["kernel_run_id"],
            "recovery_artifact_name": lock["recovery_run"]["artifact_name"],
            "source_compile_run_id": lock["source_compile_run"]["id"],
            "kernel_config_repository": source_lock["kernel"]["config_repository"],
            "kernel_config_commit": source_lock["kernel"]["config_commit"],
            "kernel_config_path": source_lock["kernel"]["config_path"],
            "kernel_config_sha256": source_lock["kernel"]["config_sha256"],
        }
        if not report["source"]["working_tree_clean"]:
            raise RuntimeError("baseline source tree must be unmodified")

        archive = one([p for p in args.recovery_dir.rglob("6.18.54.tar.gz")], "runtime archive")
        if sha256(archive) != fixed["archive_sha256"]:
            raise RuntimeError("recovered runtime archive SHA-256 mismatch")
        nested = verify_inner_checksums(archive, fixed["kernel_version"])
        if nested != fixed["inner_sha256_members"]:
            raise RuntimeError("nested kernel archive hashes differ from fixed manifest")
        header_member = one([PurePosixPath(n) for n in nested
                             if PurePosixPath(n).name.startswith("header-")], "header archive member")
        header_archive = args.out / header_member.name
        with tarfile.open(archive, "r:gz") as outer:
            member = outer.getmember(str(header_member))
            if not member.isfile():
                raise RuntimeError("header archive is not a regular file")
            with outer.extractfile(member) as src, header_archive.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        kroot = args.out / "headers"
        kroot.mkdir()
        checked_extract(header_archive, kroot)
        candidates = [p.parent.parent.parent for p in kroot.rglob("include/config/kernel.release")]
        kdir = one(candidates, "prepared kernel build directory")
        release_file = kdir / "include/config/kernel.release"
        release = release_file.read_text().strip()
        if release != fixed["kernel_release"]:
            raise RuntimeError(f"prepared kernel release mismatch: {release}")
        symvers = kdir / "Module.symvers"
        if not symvers.is_file() or symvers.stat().st_size == 0:
            raise RuntimeError("prepared header Module.symvers is missing or empty")
        report["build_inputs"] = {"kernel_release": release, "archive_sha256": fixed["archive_sha256"],
                                  "header_archive_member": str(header_member),
                                  "module_symvers_bytes": symvers.stat().st_size,
                                  "module_symvers_sha256": sha256(symvers),
                                  "toolchain_archive_sha256": sha256(args.toolchain_archive)}
        tc_lock = lock["kernel_input_manifest"]["ophub_source_lock"]["kernel_toolchain"]
        if (report["build_inputs"]["toolchain_archive_sha256"] != tc_lock["sha256"]
                or args.toolchain_archive.stat().st_size != tc_lock["size"]):
            raise RuntimeError("GNU toolchain archive SHA-256 mismatch")
        tc_root = args.out / "toolchain"
        tc_root.mkdir()
        with tarfile.open(args.toolchain_archive, "r:xz") as tf:
            for m in tf.getmembers():
                q = PurePosixPath(m.name)
                if q.is_absolute() or ".." in q.parts:
                    raise RuntimeError("unsafe path in locked toolchain archive")
            tf.extractall(tc_root, filter="data")
        compilers = list(tc_root.rglob("aarch64-none-linux-gnu-gcc"))
        compiler = one(compilers, "locked AArch64 compiler")
        version = subprocess.run([str(compiler), "--version"], text=True, capture_output=True,
                                 timeout=15, check=True).stdout.splitlines()[0]
        report["toolchain"] = {"compiler": str(compiler.relative_to(tc_root)), "version": version,
                               "sha256": tc_lock["sha256"]}
        cross = str(compiler)[:-3]
        mt76 = args.source_dir / "drivers/net/wireless/mediatek/mt76"
        targets = [("mt7615-common.ko", mt76 / "mt7615"), ("mt7663s.ko", mt76 / "mt7663s")]
        started = time.monotonic()
        for module, mdir in targets:
            if not (mdir / "Makefile").is_file():
                raise RuntimeError(f"locked source has no module Makefile: {mdir}")
            argv = ["make", "-C", str(kdir), "ARCH=arm64", f"CROSS_COMPILE={cross}",
                    f"M={mdir}", "modules"]
            if module == "mt7663s.ko":
                extra = mt76 / "mt7615/Module.symvers"
                if extra.is_file():
                    argv.append(f"KBUILD_EXTRA_SYMBOLS={extra}")
            run_logged(argv, args.out / f"build-{module}.log", 420)
        report["build_seconds"] = round(time.monotonic()-started, 2)
        for module, mdir in targets:
            path = one(list(mdir.glob(module)), module)
            vermagic = modinfo(path, "vermagic")
            if not vermagic or vermagic.split()[0] != fixed["kernel_release"]:
                raise RuntimeError(f"built {module} vermagic does not match locked kernel release")
            host_sha = next((m.get("archive_sha256") for m in
                             json.loads(args.audit_report.read_text()).get("modules", [])
                             if m.get("name").replace("_", "-") == module[:-3]), None)
            module_sha = sha256(path)
            report["modules"].append({"name": module[:-3], "path": module, "sha256": module_sha,
                                      "bytes": path.stat().st_size, "vermagic": vermagic,
                                      "depends": modinfo(path, "depends"),
                                      "baseline_host_sha256": host_sha,
                                      "host_hash_matches": module_sha == host_sha if host_sha else None,
                                      "hash_match_is_informational": True})
            shutil.copy2(path, args.out / module)
        report["baseline_build_complete"] = True
        report["limitations"].append("Only mt7615-common.ko and mt7663s.ko were requested; external-module build success is not device-load or connectivity acceptance.")
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {str(exc)[:500]}")
        report["baseline_build_complete"] = False
    (args.out / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"baseline_build_complete": report["baseline_build_complete"],
                      "modules": report.get("modules", []), "errors": report["errors"]}, separators=(",", ":")), flush=True)
    return 0 if report["baseline_build_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
