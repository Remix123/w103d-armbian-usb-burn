#!/usr/bin/env python3
"""Create and verify a ZIP delivery from an already verified USB IMG artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def regular_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in delivery package: {path}")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def build_package(image: Path, reports: Path, out: Path, zip_name: str) -> Path:
    image = image.resolve(strict=True)
    reports = reports.resolve(strict=True)
    if image.suffix != ".img" or not image.is_file() or not reports.is_dir():
        raise ValueError("input must be one raw .img and a reports directory")
    report_files = regular_files(reports)
    required = {"verification-report.json", "artifact-download-validation.txt", "assembly-input-report.json"}
    available = {path.name for path in report_files}
    missing = required - available
    if missing:
        raise ValueError(f"verification report artifact is incomplete: {sorted(missing)}")

    staging = out / "package"
    if staging.exists():
        shutil.rmtree(staging)
    staged_reports = staging / "reports"
    staged_reports.mkdir(parents=True)
    staged_image = staging / image.name
    shutil.copyfile(image, staged_image)
    for source in report_files:
        rel = source.relative_to(reports)
        destination = staged_reports / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    entries = [staged_image, *regular_files(staged_reports)]
    lines = [f"{sha256(path)}  {path.relative_to(staging).as_posix()}" for path in entries]
    (staging / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    out.mkdir(parents=True, exist_ok=True)
    archive = out / zip_name
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zf:
        for path in regular_files(staging):
            zf.write(path, path.relative_to(staging).as_posix())
    verify_archive(archive, expected_image_name=image.name, expected_image_sha256=sha256(image))
    (out / f"{archive.name}.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def verify_archive(archive: Path, expected_image_name: str | None = None, expected_image_sha256: str | None = None) -> dict:
    archive = archive.resolve(strict=True)
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"ZIP CRC verification failed for {bad}")
        infos = zf.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ValueError("ZIP contains duplicate member paths")
        for name, info in zip(names, infos):
            item = PurePosixPath(name)
            if item.is_absolute() or ".." in item.parts or "\\" in name:
                raise ValueError(f"unsafe ZIP member path: {name}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP symlink is not allowed: {name}")
        image_names = [name for name in names if name.endswith(".img") and not name.endswith("/")]
        if len(image_names) != 1:
            raise ValueError(f"expected exactly one raw IMG member, found {image_names}")
        image_name = image_names[0]
        if expected_image_name is not None and image_name != expected_image_name:
            raise ValueError(f"IMG basename mismatch: {image_name} != {expected_image_name}")
        if "SHA256SUMS" not in names:
            raise ValueError("ZIP has no internal SHA256SUMS")
        sums = zf.read("SHA256SUMS").decode("ascii").splitlines()
        expected_hashes = {}
        for line in sums:
            digest, rel = line.split("  ", 1)
            if rel in expected_hashes:
                raise ValueError(f"duplicate SHA256SUMS path: {rel}")
            expected_hashes[rel] = digest
        if image_name not in expected_hashes:
            raise ValueError("internal SHA256SUMS does not name the IMG basename")
        files = {name for name in names if not name.endswith("/")}
        if set(expected_hashes) != files - {"SHA256SUMS"}:
            raise ValueError("internal SHA256SUMS does not cover exactly all packaged payload and report files")
        for name, digest in expected_hashes.items():
            with zf.open(name) as member:
                if sha256_stream(member) != digest:
                    raise ValueError(f"internal SHA256SUMS mismatch: {name}")
        image_sha = expected_hashes[image_name]
        if expected_image_sha256 is not None and image_sha != expected_image_sha256:
            raise ValueError("ZIP IMG does not match verified input IMG SHA-256")
        return {"zip": archive.name, "zip_bytes": archive.stat().st_size, "zip_sha256": sha256(archive), "image": image_name, "image_sha256": image_sha, "members": len(names)}


def verify_artifact_download(transfer: Path, out: Path, expected_name: str, expected_zip_sha256: str, image_name: str, image_sha256: str) -> Path:
    transfer = transfer.resolve(strict=True)
    out.mkdir(parents=True, exist_ok=True)
    extracted = out / expected_name
    transfer_sha = sha256(transfer)
    if transfer_sha == expected_zip_sha256:
        shutil.copyfile(transfer, extracted)
    else:
        if not zipfile.is_zipfile(transfer):
            raise ValueError("artifact download is neither the expected raw ZIP nor a ZIP-wrapped artifact")
        with zipfile.ZipFile(transfer) as outer:
            bad = outer.testzip()
            if bad is not None:
                raise ValueError(f"outer artifact ZIP CRC failed: {bad}")
            members = [item for item in outer.infolist() if not item.is_dir() and PurePosixPath(item.filename).name == expected_name]
            if len(members) != 1:
                raise ValueError(f"outer artifact ZIP must contain exactly one {expected_name}: {[m.filename for m in members]}")
            with outer.open(members[0]) as source, extracted.open("wb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    if sha256(extracted) != expected_zip_sha256:
        raise ValueError("downloaded ZIP bytes do not match the uploaded artifact hash")
    verify_archive(extracted, image_name, image_sha256)
    return extracted


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    package = sub.add_parser("package")
    package.add_argument("--image", type=Path, required=True)
    package.add_argument("--reports", type=Path, required=True)
    package.add_argument("--out", type=Path, required=True)
    package.add_argument("--zip-name", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--zip", type=Path, required=True)
    verify.add_argument("--expected-image", required=True)
    verify.add_argument("--expected-image-sha256", required=True)
    transfer = sub.add_parser("verify-artifact-download")
    transfer.add_argument("--transfer", type=Path, required=True)
    transfer.add_argument("--out", type=Path, required=True)
    transfer.add_argument("--expected-name", required=True)
    transfer.add_argument("--expected-zip-sha256", required=True)
    transfer.add_argument("--expected-image", required=True)
    transfer.add_argument("--expected-image-sha256", required=True)
    args = parser.parse_args()
    if args.command == "package":
        archive = build_package(args.image, args.reports, args.out, args.zip_name)
        print(json.dumps(verify_archive(archive), indent=2, sort_keys=True))
    elif args.command == "verify":
        print(json.dumps(verify_archive(args.zip, args.expected_image, args.expected_image_sha256), indent=2, sort_keys=True))
    else:
        result = verify_artifact_download(args.transfer, args.out, args.expected_name, args.expected_zip_sha256, args.expected_image, args.expected_image_sha256)
        print(json.dumps(verify_archive(result, args.expected_image, args.expected_image_sha256), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
