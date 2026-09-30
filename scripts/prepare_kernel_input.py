#!/usr/bin/env python3
"""Validate and stage the exact Ophub kernel archive from a Actions run artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_members(bundle: tarfile.TarFile):
    for member in bundle.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit(f"unsafe member path in kernel archive: {member.name}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise SystemExit(f"unsupported special file in kernel archive: {member.name}")
    return bundle.getmembers()


def verify_inner_checksums(archive: Path, version: str) -> dict[str, str]:
    with tarfile.open(archive, "r:gz") as bundle:
        members = safe_members(bundle)
        files = {str(PurePosixPath(m.name)): m for m in members if m.isfile()}
        expected_prefix = version + "/"
        payloads = [name for name in files if name.startswith(expected_prefix)]
        if not payloads:
            raise SystemExit(f"kernel archive has no {version}/ payload root")
        checksums = [name for name in files if PurePosixPath(name).name == "sha256sums"]
        if len(checksums) != 1:
            raise SystemExit(f"expected one sha256sums file inside kernel archive; found {len(checksums)}")
        check_member = bundle.extractfile(files[checksums[0]])
        assert check_member is not None
        entries: dict[str, str] = {}
        for line in check_member.read().decode("utf-8").splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[* ]?(.+)", line.strip())
            if not match:
                raise SystemExit(f"malformed sha256sums line: {line!r}")
            digest, raw_name = match.groups()
            name = str(PurePosixPath(raw_name))
            if name not in files:
                # Ophub manifests commonly use paths relative to the archive root.
                name = str(PurePosixPath(checksums[0]).parent / name)
            if name not in files:
                raise SystemExit(f"sha256sums references a missing archive member: {raw_name}")
            content = bundle.extractfile(files[name])
            assert content is not None
            actual = hashlib.sha256(content.read()).hexdigest()
            if actual.lower() != digest.lower():
                raise SystemExit(f"inner SHA-256 mismatch for {name}")
            entries[name] = actual
        if not entries:
            raise SystemExit("sha256sums did not validate any kernel payload")
        required = ("boot-", "dtb-amlogic-", "modules-", "header-")
        for prefix in required:
            if not any(PurePosixPath(name).name.startswith(prefix) for name in entries):
                raise SystemExit(f"kernel archive sha256sums does not include required {prefix} archive")
        return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    locks = list(args.artifact_dir.rglob("sources.lock.json"))
    if not locks:
        raise SystemExit("downloaded kernel artifact has no sources.lock.json")
    lock_hashes = {sha256(path) for path in locks}
    if len(lock_hashes) != 1:
        raise SystemExit(f"downloaded kernel artifact contains conflicting source locks: {[str(p) for p in locks]}")
    output_locks = [path for path in locks if path.as_posix().endswith("/outputs/sources.lock.json")]
    if len(output_locks) > 1:
        raise SystemExit("kernel artifact has multiple primary outputs/sources.lock.json files")
    lock_path = output_locks[0] if output_locks else locks[0]
    lock = json.loads(lock_path.read_text())
    manifests = list(args.artifact_dir.rglob("kernel-packages.json"))
    if len(manifests) != 1:
        raise SystemExit(f"expected exactly one kernel-packages.json in downloaded artifact, found {len(manifests)}")
    package_manifest = json.loads(manifests[0].read_text())
    release = package_manifest.get("kernel_release", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+-ophub", release):
        raise SystemExit(f"invalid kernel release in package manifest: {release!r}")
    version = lock.get("kernel", {}).get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"invalid kernel version in source lock: {version!r}")
    archives = [p for p in args.artifact_dir.rglob("*.tar.gz") if p.name == f"{version}.tar.gz"]
    if len(archives) != 1:
        raise SystemExit(f"expected exactly one {version}.tar.gz in downloaded artifact, found {len(archives)}")
    archive = archives[0]
    inner = verify_inner_checksums(archive, version)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    staged_archive = args.output_dir / f"{version}.tar.gz"
    staged_lock = args.output_dir / "sources.lock.json"
    staged_packages = args.output_dir / "kernel-packages.json"
    shutil.copyfile(archive, staged_archive)
    shutil.copyfile(lock_path, staged_lock)
    shutil.copyfile(manifests[0], staged_packages)
    manifest = {
        "kernel_run_id": str(args.run_id),
        "kernel_version": version,
        "kernel_release": release,
        "archive_artifact_path": archive.relative_to(args.artifact_dir).as_posix(),
        "archive_sha256": sha256(staged_archive),
        "sources_lock_sha256": sha256(staged_lock),
        "kernel_packages_manifest_sha256": sha256(staged_packages),
        "inner_sha256_members": inner,
        "ophub_source_lock": lock,
    }
    (args.output_dir / "kernel-input-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    output = __import__("os").environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"kernel_version={version}\narchive={staged_archive}\n")
    print(json.dumps({k: manifest[k] for k in ("kernel_run_id", "kernel_version", "archive_artifact_path", "archive_sha256", "sources_lock_sha256")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
