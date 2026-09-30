#!/usr/bin/env python3
"""Verify Ophub's uniquely named W103D disk image against its exact kernel release."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from prepare_kernel_input import safe_members, verify_inner_checksums, sha256


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def nested_archive(archive: Path, prefix: str) -> tuple[str, bytes]:
    with tarfile.open(archive, "r:gz") as outer:
        items = [m for m in safe_members(outer) if m.isfile() and PurePosixPath(m.name).name.startswith(prefix)]
        if len(items) != 1:
            raise SystemExit(f"expected one {prefix}*.tar.gz payload; found {[m.name for m in items]}")
        stream = outer.extractfile(items[0])
        assert stream is not None
        return items[0].name, stream.read()


def regular_payload(blob: bytes, predicate, description: str) -> tuple[str, bytes]:
    import io
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as nested:
        members = [m for m in safe_members(nested) if m.isfile() and m.size > 0 and predicate(PurePosixPath(m.name))]
        if not members:
            raise SystemExit(f"kernel archive contains no regular {description} payload")
        member = members[0]
        stream = nested.extractfile(member)
        assert stream is not None
        return str(PurePosixPath(member.name)), stream.read()


def parse_kv(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        fields[key.strip().upper()] = value.strip().strip('"').strip("'")
    return fields


def find_exact_mount_path(mounts: list[Path], relpaths: list[Path], label: str) -> Path:
    found = []
    for mount in mounts:
        for relpath in relpaths:
            candidate = mount / relpath
            if candidate.is_file():
                found.append(candidate)
    if not found:
        raise SystemExit(f"rebuilt image is missing required installed path: {label}")
    hashes = {sha256(p) for p in found}
    if len(hashes) != 1:
        raise SystemExit(f"rebuilt image contains conflicting copies of {label}: {[str(p) for p in found]}")
    return found[0]


def gunzip_sparse(source: Path, target: Path) -> int:
    logical = 0
    with gzip.open(source, "rb") as inp, target.open("xb") as out:
        while block := inp.read(8 << 20):
            if any(block):
                out.seek(logical)
                out.write(block)
            logical += len(block)
        out.truncate(logical)
    return logical


def module_relative_path(member_name: str, release: str) -> Path:
    parts = PurePosixPath(member_name).parts
    relative_parts = None
    if parts and parts[0] == release:
        relative_parts = parts[1:]
    elif len(parts) >= 4 and parts[:2] == ("lib", "modules") and parts[2] == release:
        relative_parts = parts[3:]
    elif len(parts) >= 5 and parts[:3] == ("usr", "lib", "modules") and parts[3] == release:
        relative_parts = parts[4:]
    if relative_parts is None or not relative_parts:
        raise SystemExit(f"kernel module archive member is outside the exact release root {release}: {member_name}")
    return Path(*relative_parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--kernel-archive", type=Path, required=True)
    parser.add_argument("--kernel-manifest", type=Path, required=True)
    parser.add_argument("--base-image-lock", type=Path, required=True)
    parser.add_argument("--kernel-version", required=True)
    parser.add_argument("--dtb-name", default="meson-g12a-w103d.dtb")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    version = args.kernel_version
    base_lock = json.loads(args.base_image_lock.read_text())
    expected_armbian_version = base_lock.get("armbian_version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected_armbian_version):
        raise SystemExit(f"base image lock has no strict Armbian release version: {base_lock}")
    if base_lock.get("repository") != "ophub/amlogic-s9xxx-armbian" or not base_lock.get("asset_id") or not base_lock.get("asset_digest"):
        raise SystemExit(f"base image lock is not an immutable official Ophub release asset: {base_lock}")
    package_manifest = json.loads(args.kernel_manifest.read_text())
    release = package_manifest.get("kernel_release", "")
    if release != f"{version}-ophub":
        raise SystemExit(f"package manifest release {release!r} does not match expected {version}-ophub")
    pattern = re.compile(rf"^Armbian_.+_amlogic_s905l3a-w103d_trixie_{re.escape(version)}_(?:minimal|server)_.+\.img\.gz$")
    images = [path for path in args.images_dir.iterdir() if path.is_file() and pattern.fullmatch(path.name)]
    if len(images) != 1:
        raise SystemExit(f"expected exactly one named Armbian W103D Trixie image for {version}; found {[p.name for p in images]}")
    image = images[0]
    verify_inner_checksums(args.kernel_archive, version)
    boot_archive_name, boot_blob = nested_archive(args.kernel_archive, "boot-")
    dtb_archive_name, dtb_blob = nested_archive(args.kernel_archive, "dtb-amlogic-")
    modules_archive_name, modules_blob = nested_archive(args.kernel_archive, "modules-")
    boot_rel, boot_bytes = regular_payload(
        boot_blob,
        lambda p: p.name == f"vmlinuz-{release}",
        f"boot kernel vmlinuz-{release}",
    )
    dtb_rel, dtb_bytes = regular_payload(dtb_blob, lambda p: p.name == args.dtb_name, args.dtb_name)
    import io
    with tarfile.open(fileobj=io.BytesIO(modules_blob), mode="r:*") as modules_tar:
        module_members = [m for m in safe_members(modules_tar) if m.isfile() and ".ko" in PurePosixPath(m.name).name]
        if not module_members:
            raise SystemExit("kernel modules archive contains no regular .ko files")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "system-image-verification.json"
    with tempfile.TemporaryDirectory(prefix="w103d-system-check-") as td:
        temp = Path(td)
        disk = temp / "system.img"
        gunzip_sparse(image, disk)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        loop = run("sudo", "losetup", "--find", "--show", "--partscan", str(disk))
        mounts: list[Path] = []
        try:
            run("sudo", "udevadm", "settle", "--timeout=60")
            with disk.open("rb") as stream:
                mbr = stream.read(512)
            if len(mbr) != 512 or mbr[510:512] != b"\x55\xaa":
                raise SystemExit("rebuilt disk IMG does not contain an MBR signature")
            partition_table = json.loads(run("sudo", "sfdisk", "--json", loop))
            table = partition_table.get("partitiontable", {})
            if table.get("label") != "dos" or not table.get("partitions"):
                raise SystemExit(f"rebuilt image does not have the expected DOS/MBR partition table: {table}")
            lsblk_data = json.loads(run("sudo", "lsblk", "--json", "--paths", "--output", "NAME,TYPE", loop))
            children = lsblk_data["blockdevices"][0].get("children", [])
            partitions = []
            nodes = [p.get("node") for p in table["partitions"] if p.get("node")]
            if not nodes:
                nodes = [f"{loop}p{index}" for index in range(1, len(table["partitions"]) + 1)]
            for node in nodes:
                probe = run("sudo", "blkid", "-o", "export", node)
                fields = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
                fs_type = fields.get("TYPE", "")
                if fs_type:
                    partitions.append((node, fs_type))
            (args.output_dir / "partition-discovery.json").write_text(json.dumps({
                "loop_device": loop, "mbr_signature": mbr[510:512].hex(),
                "partition_table": table, "lsblk_children": children,
                "recognized_filesystems": partitions,
            }, indent=2) + "\n")
            if not partitions:
                raise SystemExit(f"rebuilt image MBR partitions have no blkid-recognized filesystems: {children}")
            for index, (partition, filesystem) in enumerate(partitions):
                mountpoint = temp / f"mnt-{index}"
                mountpoint.mkdir()
                mount_options = "ro,noload" if filesystem == "ext4" else "ro"
                subprocess.run(["sudo", "mount", "-o", mount_options, partition, str(mountpoint)], check=True)
                mounts.append(mountpoint)

            os_release = find_exact_mount_path(mounts, [Path("etc/os-release")], "/etc/os-release")
            os_fields = parse_kv(os_release)
            if os_fields.get("VERSION_CODENAME") != "trixie" or "debian" not in (os_fields.get("ID", "") + " " + os_fields.get("ID_LIKE", "")).lower():
                raise SystemExit(f"rebuilt rootfs is not Debian Trixie: {os_fields}")
            armbian_release = find_exact_mount_path(mounts, [Path("etc/armbian-release")], "/etc/armbian-release")
            armbian_fields = parse_kv(armbian_release)
            armbian_version = armbian_fields.get("VERSION", "")
            if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", armbian_version):
                raise SystemExit(f"/etc/armbian-release has no supported numeric VERSION: {armbian_fields}")
            if armbian_version != expected_armbian_version:
                raise SystemExit(f"rebuilt /etc/armbian-release VERSION {armbian_version} differs from locked Ophub base release {expected_armbian_version}")
            ophub_release = find_exact_mount_path(mounts, [Path("etc/ophub-release")], "/etc/ophub-release")
            ophub_fields = parse_kv(ophub_release)
            board_values = " ".join(v for k, v in ophub_fields.items() if "BOARD" in k).lower()
            platform_values = " ".join(v for k, v in ophub_fields.items() if "PLATFORM" in k).lower()
            if "s905l3a-w103d" not in board_values or "amlogic" not in platform_values:
                raise SystemExit(f"/etc/ophub-release does not identify W103D/Amlogic: {ophub_fields}")

            boot_path = find_exact_mount_path(
                mounts,
                [Path("boot") / f"vmlinuz-{release}", Path(f"vmlinuz-{release}")],
                f"/boot/vmlinuz-{release}",
            )
            dtb_name = Path(args.dtb_name)
            dtb_path = find_exact_mount_path(
                mounts,
                [Path("boot/dtb/amlogic") / dtb_name, Path("dtb/amlogic") / dtb_name,
                 Path("boot/dtb") / dtb_name, Path("dtb") / dtb_name],
                f"W103D DTB {args.dtb_name}",
            )
            if sha256(boot_path) != hashlib.sha256(boot_bytes).hexdigest():
                raise SystemExit("installed /boot kernel image differs from the exact Ophub release archive")
            if sha256(dtb_path) != hashlib.sha256(dtb_bytes).hexdigest():
                raise SystemExit("installed W103D DTB differs from the exact Ophub release archive")

            module_root_candidates = []
            for mount in mounts:
                module_root_candidates.extend((mount / "lib/modules" / release, mount / "usr/lib/modules" / release))
            module_root = next((path for path in module_root_candidates if path.is_dir()), None)
            if module_root is None:
                raise SystemExit(f"rebuilt rootfs does not include /lib/modules/{release}")
            module_hashes = []
            with tarfile.open(fileobj=io.BytesIO(modules_blob), mode="r:*") as modules_tar:
                for member in module_members:
                    relative_path = module_relative_path(member.name, release)
                    actual = module_root / relative_path
                    if not actual.is_file():
                        raise SystemExit(f"rebuilt image is missing kernel module {member.name} at {actual}")
                    stream = modules_tar.extractfile(member)
                    assert stream is not None
                    expected_hash = hashlib.sha256(stream.read()).hexdigest()
                    actual_hash = sha256(actual)
                    if actual_hash != expected_hash:
                        raise SystemExit(f"installed kernel module differs from exact Ophub archive: {member.name}")
                    module_hashes.append({"archive_member": member.name, "installed_path": str(actual), "sha256": actual_hash})

            report = {
                "image_file": image.name,
                "image_size": image.stat().st_size,
                "image_sha256": sha256(image),
                "kernel_version": version,
                "kernel_release": release,
                "armbian_version": armbian_version,
                "armbian_release": armbian_fields,
                "base_image": {key: base_lock[key] for key in ("repository", "release_tag", "release_id", "asset_id", "filename", "asset_size", "asset_digest", "sha256", "armbian_version")},
                "os_release": os_fields,
                "ophub_release": ophub_fields,
                "kernel_archives": {"boot": boot_archive_name, "dtb": dtb_archive_name, "modules": modules_archive_name},
                "byte_identical_boot_kernel": {"archive_member": boot_rel, "installed_path": str(boot_path), "sha256": sha256(boot_path)},
                "byte_identical_w103d_dtb": {"archive_member": dtb_rel, "installed_path": str(dtb_path), "sha256": sha256(dtb_path)},
                "byte_identical_modules_count": len(module_hashes),
                "byte_identical_modules": module_hashes,
            }
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        finally:
            for mountpoint in reversed(mounts):
                subprocess.run(["sudo", "umount", str(mountpoint)], check=False)
            subprocess.run(["sudo", "losetup", "-d", loop], check=False)
    shutil.copy2(image, args.output_dir / image.name)
    print(report_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
