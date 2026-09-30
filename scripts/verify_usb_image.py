#!/usr/bin/env python3
"""Independently unpack and validate a complete W103D USB Burning Tool IMG."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import subprocess
import tarfile
import tempfile
import zlib

from prepare_kernel_input import safe_members, sha256, verify_inner_checksums
from verify_rebuilt_system import module_relative_path
from inspect_reference import sparse_to_raw


def run(*argv: str | Path) -> str:
    return subprocess.run([str(x) for x in argv], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()


def one(items, description):
    if len(items) != 1:
        raise SystemExit(f"expected one {description}, found {len(items)}")
    return items[0]


def verify_legacy_image(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) < 64:
        raise SystemExit(f"truncated U-Boot legacy image: {path.name}")
    magic, header_crc, _, size, _, _, data_crc = struct.unpack_from(">7I", data)
    header = bytearray(data[:64])
    header[4:8] = bytes(4)
    payload = data[64:64 + size]
    if magic != 0x27051956 or len(payload) != size or len(data) != 64 + size:
        raise SystemExit(f"invalid U-Boot legacy image header: {path.name}")
    if zlib.crc32(header) != header_crc or zlib.crc32(payload) != data_crc:
        raise SystemExit(f"U-Boot legacy image CRC mismatch: {path.name}")
    return payload


def verify_script_image(path: Path, command_path: Path) -> None:
    payload = verify_legacy_image(path)
    command = command_path.read_bytes()
    # mkimage's IH_TYPE_SCRIPT payload is a network-order length, a zero
    # terminator, then the exact bytes of the sole command file. U-Boot mkimage
    # only pads non-final members of multi-file scripts, never the sole/final one.
    if len(payload) < 8:
        raise SystemExit(f"U-Boot script table is truncated: {path.name}")
    command_size, terminator = struct.unpack_from(">II", payload)
    if (terminator != 0 or command_size != len(command) or
            len(payload) != 8 + command_size or payload[8:] != command):
        raise SystemExit(f"U-Boot script payload does not match readable source: {path.name}")


def uncompressed_module_bytes(path: Path) -> bytes:
    """Normalize kernel modules that the initramfs stores with a compression suffix."""
    import gzip
    import lzma
    data = path.read_bytes()
    if path.name.endswith(".xz"):
        return lzma.decompress(data)
    if path.name.endswith(".gz"):
        return gzip.decompress(data)
    if path.name.endswith(".zst"):
        return subprocess.run(["zstd", "-q", "-d", "-c", str(path)], check=True,
                              stdout=subprocess.PIPE).stdout
    return data


def matching_module(root: Path, relative: Path) -> Path | None:
    candidates = [root / relative] + [root / (str(relative) + suffix) for suffix in (".xz", ".zst", ".gz")]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def normalized_module_name(name: str) -> str:
    for suffix in (".xz", ".zst", ".gz"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--assembly-report", type=Path, required=True)
    ap.add_argument("--kernel-archive", type=Path, required=True)
    ap.add_argument("--kernel-manifest", type=Path, required=True)
    ap.add_argument("--sources-lock", type=Path, required=True)
    ap.add_argument("--base-image-lock", type=Path, required=True)
    ap.add_argument("--reference-layout", type=Path, required=True)
    ap.add_argument("--packer", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    assembly = json.loads(a.assembly_report.read_text())
    kernel = json.loads(a.kernel_manifest.read_text())
    sources_lock = json.loads(a.sources_lock.read_text())
    base_image_lock = json.loads(a.base_image_lock.read_text())
    layout = json.loads(a.reference_layout.read_text())
    image = a.image.resolve(strict=True)
    digest = sha256(image)
    if assembly.get("output", {}).get("sha256") != digest:
        raise SystemExit("final IMG hash differs from assembly report")
    if kernel.get("archive_sha256") != sha256(a.kernel_archive):
        raise SystemExit("kernel archive differs from the recorded source build")
    if assembly.get("source_lock") != kernel.get("ophub_source_lock") or sources_lock != kernel.get("ophub_source_lock"):
        raise SystemExit("assembly provenance source lock differs from kernel build manifest")
    if assembly.get("source_lock_sha256") != sha256(a.sources_lock):
        raise SystemExit("assembly source lock file hash differs from the retained lock input")
    if assembly.get("base_image_lock") != base_image_lock or assembly.get("base_image_lock_sha256") != sha256(a.base_image_lock):
        raise SystemExit("base image provenance does not match the retained Trixie base lock")
    version, release = kernel["kernel_version"], kernel["kernel_release"]
    if release != version + "-ophub" or assembly.get("kernel_release") != release:
        raise SystemExit("kernel release disagreement between source, assembly and final verification")
    verify_inner_checksums(a.kernel_archive, version)
    with tarfile.open(a.kernel_archive, "r:gz") as outer:
        boot_bundle = one([m for m in safe_members(outer) if m.isfile() and PurePosixPath(m.name).name.startswith("boot-")], "boot archive")
        boot_stream = outer.extractfile(boot_bundle)
        assert boot_stream
        with tarfile.open(fileobj=io.BytesIO(boot_stream.read()), mode="r:*") as inner:
            config_member = one([m for m in safe_members(inner) if m.isfile() and PurePosixPath(m.name).name == f"config-{release}"], "kernel config")
            config_stream = inner.extractfile(config_member)
            assert config_stream
            config_text = config_stream.read().decode("utf-8", "strict")
    config_options = {key: next((line.split("=", 1)[1] for line in config_text.splitlines() if line.startswith(key + "=")),
                                "n" if f"# {key} is not set" in config_text else "missing")
                      for key in ("CONFIG_MMC_MESON_GX", "CONFIG_EXT4_FS")}
    if (config_options != assembly.get("kernel_config_options") or
            hashlib.sha256(config_text.encode()).hexdigest() != assembly.get("kernel_config_sha256")):
        raise SystemExit("kernel config evidence differs from the locked kernel archive")
    with tempfile.TemporaryDirectory(prefix="w103d-final-check-") as td:
        root = Path(td)
        extracted = root / "container"
        extracted.mkdir()
        run(a.packer, "-c", image)
        run(a.packer, "-d", image, extracted)
        if (extracted / "image.cfg").read_text() != layout["image_cfg"]:
            raise SystemExit("final container image.cfg differs from the reviewed reference layout")
        parts = {p.name: p for p in extracted.iterdir() if p.is_file()}
        expected_names = {"DDR.USB", "_aml_dtb.PARTITION", "boot.PARTITION", "recovery.PARTITION", "dtbo.PARTITION",
                          "vbmeta.PARTITION", "logo.PARTITION", "platform.conf", "system.PARTITION", "data.PARTITION"}
        if not expected_names.issubset(parts):
            raise SystemExit(f"final container is missing payloads: {sorted(expected_names - set(parts))}")
        preserved = {}
        for name, expected in layout["component_sha256"].items():
            actual = sha256(parts[name])
            if actual != expected:
                raise SystemExit(f"preserved vendor payload hash changed: {name}")
            preserved[name] = actual
        # Re-read both sparse partition payloads; check Android sparse CRC and full raw SHA.
        raw_boot, raw_root = root / "bootfs.raw", root / "rootfs.raw"
        boot_info = sparse_to_raw(parts["system.PARTITION"], raw_boot)
        root_info = sparse_to_raw(parts["data.PARTITION"], raw_root)
        if boot_info["sha256"] != assembly["sparse_roundtrip"]["system"]["raw_sha256"]:
            raise SystemExit("FAT payload differs from assembled raw filesystem")
        if root_info["sha256"] != assembly["sparse_roundtrip"]["data"]["raw_sha256"]:
            raise SystemExit("ext4 payload differs from assembled raw filesystem")
        fat_readback = root / "fat-readback"
        fat_readback.mkdir()
        run("mcopy", "-s", "-i", raw_boot, "::*", str(fat_readback) + "/")
        boot_file_checks = {}
        for relative, expected_hash in assembly["boot_files_sha256"].items():
            file = fat_readback / relative
            if not file.is_file() or sha256(file) != expected_hash:
                raise SystemExit(f"FAT readback differs from recorded boot payload: {relative}")
            boot_file_checks[relative] = expected_hash
        with tarfile.open(a.kernel_archive, "r:gz") as outer:
            member = one([m for m in safe_members(outer) if m.isfile() and PurePosixPath(m.name).name.startswith("boot-")], "boot archive")
            payload = outer.extractfile(member)
            assert payload
            with tarfile.open(fileobj=io.BytesIO(payload.read()), mode="r:*") as inner:
                kernel_member = one([m for m in safe_members(inner) if m.isfile() and PurePosixPath(m.name).name == f"vmlinuz-{release}"], "release kernel")
                stream = inner.extractfile(kernel_member)
                assert stream
                kernel_hash = hashlib.sha256(stream.read()).hexdigest()
                if kernel_hash != sha256(fat_readback / "zImage") or kernel_hash != sha256(fat_readback / "Image"):
                    raise SystemExit("final FAT kernel is not byte-identical to the locked Ophub kernel")
        dtb = fat_readback / "dtb-w103d/meson-g12a-w103d.dtb"
        if not dtb.is_file():
            raise SystemExit("final FAT is missing the W103D DTB at vendor boot path")
        with tarfile.open(a.kernel_archive, "r:gz") as outer:
            member = one([m for m in safe_members(outer) if m.isfile() and PurePosixPath(m.name).name.startswith("dtb-amlogic-")], "DTB archive")
            stream = outer.extractfile(member)
            assert stream
            with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:*") as inner:
                expected_dtb = one([m for m in safe_members(inner) if m.isfile() and PurePosixPath(m.name).name == dtb.name], "W103D DTB")
                data = inner.extractfile(expected_dtb)
                assert data
                if hashlib.sha256(data.read()).hexdigest() != sha256(dtb):
                    raise SystemExit("final W103D DTB differs from locked source archive")
        mainline_cmd_path = fat_readback / "boot-emmc.cmd"
        mainline_cmd = mainline_cmd_path.read_text()
        if (sha256(mainline_cmd_path) != assembly.get("ophub_boot_emmc_cmd_sha256") or
                mainline_cmd != (fat_readback / "boot.cmd").read_text() or
                "booti ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}" not in mainline_cmd or
                "${INITRD}" not in mainline_cmd or "initrd_addr" in mainline_cmd):
            raise SystemExit("mainline eMMC script differs from locked Ophub source or changed U-Boot variable semantics")
        if "root=LABEL=W103D_ROOT" not in (fat_readback / "uEnv.txt").read_text():
            raise SystemExit("final uEnv root selector is not the ext4 filesystem label")
        vendor_cmd = (fat_readback / "emmc_autoscript.cmd").read_text()
        for token in ("root=LABEL=W103D_ROOT", "uInitrd", "dtb-w103d/meson-g12a-w103d.dtb", "booti "):
            if token not in vendor_cmd:
                raise SystemExit(f"vendor fallback script is missing expected boot path/argument: {token}")
        vendor_cmd_path = fat_readback / "emmc_autoscript.cmd"
        if "setenv initrd_addr" not in vendor_cmd or "ramdisk_addr_r" in vendor_cmd:
            raise SystemExit("vendor fallback lacks the reviewed initrd_addr compatibility fix")
        verify_script_image(fat_readback / "emmc_autoscript", vendor_cmd_path)
        verify_script_image(fat_readback / "boot.scr", fat_readback / "boot.cmd")
        if (fat_readback / "uInitrd").exists():
            verify_legacy_image(fat_readback / "uInitrd")
        boot_sector = raw_boot.open("rb").read(512)
        if boot_sector[71:82].rstrip(b" ") != b"W103D_BOOT":
            raise SystemExit("read-back FAT volume label mismatch")
        run("fsck.vfat", "-n", raw_boot)
        label = run("e2label", raw_root)
        if label != "W103D_ROOT":
            raise SystemExit(f"read-back ext4 volume label mismatch: {label}")
        run("e2fsck", "-fn", raw_root)
        mountpoint = root / "root-readback"
        mountpoint.mkdir()
        subprocess.run(["mount", "-o", "loop,ro,noload", str(raw_root), str(mountpoint)], check=True)
        try:
            os_release = (mountpoint / "etc/os-release").read_text(errors="replace")
            ophub_release = (mountpoint / "etc/ophub-release").read_text(errors="replace")
            if "VERSION_CODENAME=trixie" not in os_release or "DISK_TYPE='emmc'" not in ophub_release:
                raise SystemExit("rootfs readback is not Trixie/emmc")
            fstab = (mountpoint / "etc/fstab").read_text()
            if "LABEL=W103D_ROOT / ext4" not in fstab or "LABEL=W103D_BOOT /boot vfat" not in fstab:
                raise SystemExit("rootfs /etc/fstab does not mount the measured W103D labels")
            if (mountpoint / "etc/machine-id").read_bytes():
                raise SystemExit("machine-id was not cleared for first boot")
            if not (mountpoint / "var/lib/dbus/machine-id").is_symlink() or os.readlink(mountpoint / "var/lib/dbus/machine-id") != "/etc/machine-id":
                raise SystemExit("D-Bus machine-id does not follow the regenerated system machine-id")
            if list((mountpoint / "etc/ssh").glob("ssh_host_*")):
                raise SystemExit("SSH host private/public keys were not cleared")
            service_link = mountpoint / "etc/systemd/system/multi-user.target.wants/w103d-resize-rootfs.service"
            if not service_link.is_symlink() or os.readlink(service_link) != "../w103d-resize-rootfs.service":
                raise SystemExit("W103D first-boot resize service is not enabled")
            resize = mountpoint / "usr/local/sbin/w103d-resize-rootfs"
            if not resize.is_file() or stat.S_IMODE(resize.stat().st_mode) != 0o755 or "W103D_ROOT" not in resize.read_text():
                raise SystemExit("W103D resize script is missing its executable mode or label guard")
            module_root = mountpoint / "lib/modules" / release
            if not module_root.is_dir():
                module_root = mountpoint / "usr/lib/modules" / release
            if not module_root.is_dir():
                raise SystemExit(f"rootfs does not contain /lib/modules/{release}")
            module_records = []
            module_names = set()
            with tarfile.open(a.kernel_archive, "r:gz") as outer:
                candidates = [m for m in safe_members(outer) if m.isfile() and PurePosixPath(m.name).name.startswith("modules-")]
                module_archive = one(candidates, "modules archive")
                stream = outer.extractfile(module_archive)
                assert stream
                with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:*") as inner:
                    regular = [m for m in safe_members(inner) if m.isfile() and ".ko" in PurePosixPath(m.name).name]
                    if not regular:
                        raise SystemExit("locked kernel archive contains no module files")
                    for member in regular:
                        relative = module_relative_path(member.name, release)
                        base_name = relative.name
                        for suffix in (".xz", ".zst", ".gz"):
                            if base_name.endswith(suffix):
                                base_name = base_name[:-len(suffix)]
                        module_names.add(base_name)
                        actual = matching_module(module_root, relative)
                        if actual is None:
                            raise SystemExit(f"rootfs is missing exact release module: {member.name}")
                        payload = inner.extractfile(member)
                        assert payload
                        expected_hash = hashlib.sha256(payload.read()).hexdigest()
                        actual_hash = hashlib.sha256(uncompressed_module_bytes(actual)).hexdigest()
                        if expected_hash != actual_hash:
                            raise SystemExit(f"rootfs module differs from exact Ophub build: {member.name}")
                        module_records.append({"member": member.name, "sha256": actual_hash})
            configured_module = {"CONFIG_MMC_MESON_GX": "meson-gx-mmc.ko", "CONFIG_EXT4_FS": "ext4.ko"}
            config_options = assembly.get("kernel_config_options", {})
            required_initrd_modules = {module for option, module in configured_module.items()
                                       if config_options.get(option) == "m"}
            missing_package_modules = required_initrd_modules - module_names
            if missing_package_modules:
                raise SystemExit(f"kernel config marks modules required but locked module archive lacks them: {sorted(missing_package_modules)}")
            initrd_dir = root / "initramfs-unpacked"
            initrd_dir.mkdir()
            initrd_payload = verify_legacy_image(fat_readback / "uInitrd")
            raw_initrd = root / "initrd.raw"
            raw_initrd.write_bytes(initrd_payload)
            subprocess.run(["unmkinitramfs", str(raw_initrd), str(initrd_dir)], check=True)
            initrd_module_paths = [p for p in initrd_dir.rglob("*") if p.is_file() and
                                   any(p.name.endswith(suffix) for suffix in (".ko", ".ko.xz", ".ko.zst", ".ko.gz"))]
            if not initrd_module_paths:
                raise SystemExit("unmkinitramfs found no kernel modules in the W103D boot initramfs")
            initrd_modules = 0
            initrd_module_names = set()
            for path in initrd_module_paths:
                relative_text = path.as_posix()
                marker = f"/lib/modules/{release}/"
                if marker not in relative_text:
                    raise SystemExit(f"initramfs module is outside locked release {release}: {path}")
                relative_text = relative_text.split(marker, 1)[1]
                for suffix in (".xz", ".zst", ".gz"):
                    if relative_text.endswith(suffix):
                        relative_text = relative_text[:-len(suffix)]
                        break
                relative = Path(relative_text)
                initrd_module_names.add(normalized_module_name(relative.name))
                root_module = matching_module(module_root, relative)
                if root_module is None or hashlib.sha256(uncompressed_module_bytes(path)).digest() != hashlib.sha256(uncompressed_module_bytes(root_module)).digest():
                    raise SystemExit(f"initramfs module differs from its final rootfs copy: {path}")
                initrd_modules += 1
            if required_initrd_modules - initrd_module_names:
                raise SystemExit(f"kernel config requires modules in initramfs but boot initramfs is missing them: {sorted(required_initrd_modules - initrd_module_names)}")
        finally:
            subprocess.run(["umount", str(mountpoint)], check=True)
        report = {
            "final_image": image.name, "bytes": image.stat().st_size, "sha256": digest,
            "container_check": "passed", "unpacked_payloads": sorted(parts),
            "preserved_vendor_payload_sha256": preserved,
            "sparse_roundtrip": {"system": boot_info, "data": root_info},
            "fat_regular_files_checked": len(boot_file_checks), "kernel_release": release,
            "kernel_modules_byte_identical": len(module_records), "initramfs_modules_byte_identical": initrd_modules,
            "root_filesystem": {"label": "W103D_ROOT", "read_only_mount": True, "e2fsck": "passed"},
            "u_boot_scripts": "mkimage legacy header and path configuration checked",
            "hardware_flashed": False, "hardware_boot_tested": False,
        }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
