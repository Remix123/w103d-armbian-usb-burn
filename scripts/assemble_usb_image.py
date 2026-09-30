#!/usr/bin/env python3
"""Assemble the verified Ophub W103D system into the measured USB-burn layout.

All disk-image reads/writes happen in a Linux GitHub Actions runner. The source
disk and reference archive are inputs only; a new raw Amlogic image is emitted.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tarfile
import zlib

from prepare_kernel_input import safe_members, sha256

VENDOR_COMPONENTS = [
    "DDR.USB", "_aml_dtb.PARTITION", "boot.PARTITION", "recovery.PARTITION",
    "dtbo.PARTITION", "vbmeta.PARTITION", "logo.PARTITION", "platform.conf",
]


def run(*argv: str | Path, capture: bool = True) -> str:
    result = subprocess.run([str(x) for x in argv], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    return result.stdout.strip() if capture else ""


def hash_file(path: Path) -> str:
    return sha256(path)


def read_release_payload(archive: Path, version: str, prefix: str, basename: str) -> bytes:
    with tarfile.open(archive, "r:gz") as outer:
        outer_members = safe_members(outer)
        matches = [m for m in outer_members if m.isfile() and PurePosixPath(m.name).name.startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"expected one {prefix}* archive; found {[m.name for m in matches]}")
        stream = outer.extractfile(matches[0])
        assert stream is not None
        import io
        with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:*") as inner:
            files = [m for m in safe_members(inner) if m.isfile() and PurePosixPath(m.name).name == basename]
            if len(files) != 1:
                raise SystemExit(f"expected one {basename} in {matches[0].name}; found {[m.name for m in files]}")
            data = inner.extractfile(files[0])
            assert data is not None
            return data.read()


def mounted_partitions(image: Path, root: Path) -> tuple[str, list[tuple[str, Path]]]:
    """Mount source image filesystems read-only and return root mount + mounts."""
    loop = run("losetup", "--find", "--show", "--partscan", str(image))
    mounts: list[tuple[str, Path]] = []
    try:
        run("udevadm", "settle", "--timeout=60")
        table = json.loads(run("sfdisk", "--json", loop)).get("partitiontable", {})
        if table.get("label") != "dos" or not table.get("partitions"):
            raise SystemExit(f"rebuilt source image does not have the measured DOS/MBR table: {table}")
        nodes = [p.get("node") for p in table["partitions"] if p.get("node")]
        if not nodes:
            nodes = [f"{loop}p{index}" for index in range(1, len(table["partitions"]) + 1)]
        for index, node in enumerate(nodes):
            probe = run("blkid", "-o", "export", node)
            fields = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
            filesystem = fields.get("TYPE", "")
            if not filesystem:
                continue
            point = root / f"source-{index}"
            point.mkdir()
            options = "ro,noload" if filesystem == "ext4" else "ro"
            subprocess.run(["mount", "-o", options, node, str(point)], check=True)
            mounts.append((node, point))
        roots = [mount for _, mount in mounts if (mount / "etc/os-release").is_file()]
        if len(roots) != 1:
            raise SystemExit(f"expected one mounted system root with /etc/os-release; found {roots}")
        return loop, mounts
    except BaseException:
        for _, point in reversed(mounts):
            subprocess.run(["umount", str(point)], check=False)
        subprocess.run(["losetup", "-d", loop], check=False)
        raise


def unmount_all(loop: str, mounts: list[tuple[str, Path]]) -> None:
    for _, point in reversed(mounts):
        subprocess.run(["umount", str(point)], check=True)
    subprocess.run(["losetup", "-d", loop], check=True)


def raw_to_sparse(source: Path, target: Path) -> dict:
    size = source.stat().st_size
    block = 4096
    if size == 0 or size % block:
        raise SystemExit(f"raw partition size is not aligned to {block}: {size}")
    chunks = 0
    crc = 0
    with source.open("rb") as inp, target.open("xb") as out:
        out.write(bytes(28))
        while data := inp.read(1 << 20):
            if len(data) % block:
                raise SystemExit("sparse conversion encountered an unaligned chunk")
            crc = zlib.crc32(data, crc)
            if not any(data):
                out.write(struct.pack("<HHII", 0xCAC2, 0, len(data) // block, 16))
                out.write(bytes(4))
            else:
                out.write(struct.pack("<HHII", 0xCAC1, 0, len(data) // block, 12 + len(data)))
                out.write(data)
            chunks += 1
        out.seek(0)
        out.write(struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, block, size // block, chunks, crc))
    return {"raw_bytes": size, "raw_sha256": sha256(source), "sparse_sha256": sha256(target), "crc32": f"{crc:08x}"}


def raw_initrd(path: Path) -> bytes:
    data = path.read_bytes()
    if not data.startswith(b"\x27\x05\x19\x56"):
        return data
    if len(data) < 64:
        raise SystemExit("uInitrd has a truncated U-Boot legacy header")
    magic, header_crc, _, size, _, _, data_crc = struct.unpack_from(">7I", data)
    header = bytearray(data[:64])
    header[4:8] = bytes(4)
    payload = data[64:64 + size]
    if magic != 0x27051956 or len(payload) != size or zlib.crc32(header) != header_crc or zlib.crc32(payload) != data_crc:
        raise SystemExit("uInitrd legacy header or payload CRC is invalid")
    return payload


def ensure_uinitrd(source_boot: Path, source_initrd: Path, output: Path) -> bytes:
    payload = raw_initrd(source_initrd)
    existing = source_boot / "uInitrd"
    if existing.is_file():
        wrapped = existing.read_bytes()
        if raw_initrd(existing) != payload:
            raise SystemExit("Ophub uInitrd payload differs from the selected initrd")
        output.write_bytes(wrapped)
    else:
        raw = output.with_suffix(".raw")
        raw.write_bytes(payload)
        run("mkimage", "-A", "arm", "-O", "linux", "-T", "ramdisk", "-C", "none",
            "-n", "W103D initramfs", "-d", raw, output)
        raw.unlink()
        if raw_initrd(output) != payload:
            raise SystemExit("generated uInitrd did not preserve the source initrd payload")
    return payload


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference-dir", type=Path, required=True)
    ap.add_argument("--system-image", type=Path, required=True, help="verified Ophub .img.gz")
    ap.add_argument("--system-report", type=Path, required=True)
    ap.add_argument("--kernel-archive", type=Path, required=True)
    ap.add_argument("--kernel-manifest", type=Path, required=True)
    ap.add_argument("--sources-lock", type=Path, required=True)
    ap.add_argument("--base-image-lock", type=Path, required=True)
    ap.add_argument("--ophub-boot-emmc", type=Path, required=True)
    ap.add_argument("--packer", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    for p in (a.reference_dir, a.system_image, a.system_report, a.kernel_archive, a.kernel_manifest,
              a.sources_lock, a.base_image_lock, a.ophub_boot_emmc, a.packer):
        if not p.exists():
            raise SystemExit(f"required input does not exist: {p}")
    output_template = a.output.as_posix()
    if "{armbian_version}" not in output_template or "{kernel_version}" not in output_template or a.work.exists():
        raise SystemExit("work must be new and output path must contain both version placeholders")
    a.work.mkdir(parents=True)
    checks = a.work / "checks"
    checks.mkdir()
    reference = a.work / "reference"
    reference.mkdir()
    run(a.packer, "-c", a.reference_dir / "W103D_Armbian_26.8.1_Server.img")
    run(a.packer, "-d", a.reference_dir / "W103D_Armbian_26.8.1_Server.img", reference)
    for name in VENDOR_COMPONENTS:
        if not (reference / name).is_file():
            raise SystemExit(f"reference packer output is missing preserved component {name}")
    layout = json.loads(Path("config/reference-layout-lock.json").read_text())
    if (reference / "image.cfg").read_text() != layout["image_cfg"]:
        raise SystemExit("reference image.cfg differs from the preflight-approved exact layout")
    hashes = layout["component_sha256"]
    for name in VENDOR_COMPONENTS:
        if hash_file(reference / name) != hashes[name]:
            raise SystemExit(f"reference component no longer matches reviewed preflight: {name}")

    manifest = json.loads(a.kernel_manifest.read_text())
    source_lock = json.loads(a.sources_lock.read_text())
    base_image_lock = json.loads(a.base_image_lock.read_text())
    if source_lock != manifest.get("ophub_source_lock"):
        raise SystemExit("standalone sources.lock differs from the kernel manifest source lock")
    release = manifest["kernel_release"]
    version = manifest["kernel_version"]
    if release != version + "-ophub" or hash_file(a.kernel_archive) != manifest["archive_sha256"]:
        raise SystemExit("kernel archive or kernel release differs from its verified manifest")
    system_report = json.loads(a.system_report.read_text())
    if system_report.get("image_sha256") != hash_file(a.system_image):
        raise SystemExit("system image hash does not match its successful rebuild verification report")
    if system_report.get("kernel_release") != release or system_report.get("kernel_version") != version:
        raise SystemExit("rebuilt system report identifies a different kernel release")
    kernel_image = read_release_payload(a.kernel_archive, version, "boot-", f"vmlinuz-{release}")
    kernel_config = read_release_payload(a.kernel_archive, version, "boot-", f"config-{release}").decode("utf-8", "strict")
    config_options = {key: next((line.split("=", 1)[1] for line in kernel_config.splitlines() if line.startswith(key + "=")),
                                "n" if f"# {key} is not set" in kernel_config else "missing")
                      for key in ("CONFIG_MMC_MESON_GX", "CONFIG_EXT4_FS")}
    if any(value not in ("y", "m") for value in config_options.values()):
        raise SystemExit(f"kernel config lacks W103D MMC/ext4 support: {config_options}")
    dtb = read_release_payload(a.kernel_archive, version, "dtb-amlogic-", "meson-g12a-w103d.dtb")

    unpacked_system = a.work / "rebuilt-system.img"
    gunzip_sparse(a.system_image, unpacked_system)
    loop, mounts = mounted_partitions(unpacked_system, a.work)
    source_root = next(point for _, point in mounts if (point / "etc/os-release").is_file())
    armbian_release = source_root / "etc/armbian-release"
    if not armbian_release.is_file():
        raise SystemExit("rebuilt system root has no /etc/armbian-release version metadata")
    armbian_fields = {}
    for line in armbian_release.read_text(errors="replace").splitlines():
        line=line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value=line.split("=",1)
            armbian_fields[key.strip().upper()]=value.strip().strip('"').strip("'")
    armbian_version=armbian_fields.get("VERSION", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", armbian_version):
        raise SystemExit(f"system /etc/armbian-release VERSION is invalid: {armbian_fields}")
    a.output = Path(output_template.replace("{armbian_version}", armbian_version).replace("{kernel_version}", version))
    if a.output.exists():
        raise SystemExit(f"versioned output already exists: {a.output}")
    if system_report.get("armbian_version") not in (None, armbian_version):
        raise SystemExit("mounted system /etc/armbian-release version differs from verified system report")
    source_boot_roots = [source_root / "boot"]
    for _, point in mounts:
        source_boot_roots.extend((point, point / "boot"))
    source_boot_roots = list(dict.fromkeys(p.resolve() for p in source_boot_roots))
    if not (source_root / "boot").is_dir():
        raise SystemExit("rebuilt root filesystem has no /boot directory")

    # The approved reference has exact 1 GiB FAT and 4 GiB ext4 raw system/data payloads.
    boot_raw = a.work / "bootfs.raw"
    with boot_raw.open("xb") as f:
        f.truncate(1 << 30)
    run("mformat", "-i", boot_raw, "-F", "-v", "W103D_BOOT", "-N", "103d6181", "::")
    root_raw = a.work / "rootfs.raw"
    with root_raw.open("xb") as f:
        f.truncate(4 << 30)
    run("mkfs.ext4", "-F", "-L", "W103D_ROOT", "-U", "01b94932-6a4a-4c81-9a71-20bd55b675a8", root_raw)
    root_mount = a.work / "new-root"
    root_mount.mkdir()
    subprocess.run(["mount", "-o", "loop", str(root_raw), str(root_mount)], check=True)
    try:
        run("rsync", "-aHAX", "--numeric-ids", "--exclude=/boot/*", f"{source_root}/", f"{root_mount}/", capture=False)
        if shutil.disk_usage(root_mount).used > (4 << 30) * 0.92:
            raise SystemExit("rebuilt root filesystem payload leaves insufficient ext4 reserve")
        fstab = root_mount / "etc/fstab"
        fstab.write_text("LABEL=W103D_ROOT / ext4 defaults,noatime,nodiratime,errors=remount-ro 0 1\nLABEL=W103D_BOOT /boot vfat defaults 0 2\ntmpfs /tmp tmpfs defaults,nosuid 0 0\n")
        (root_mount / "root/.no_rootfs_resize").write_text("no\n")
        for path in (root_mount / "etc/machine-id", root_mount / "var/lib/dbus/machine-id"):
            path.unlink(missing_ok=True)
        (root_mount / "etc/machine-id").touch()
        (root_mount / "etc/machine-id").chmod(0o444)
        host_keys = list((root_mount / "etc/ssh").glob("ssh_host_*"))
        for path in host_keys:
            path.unlink(missing_ok=True)
        (root_mount / "etc/systemd/system/ssh.service.d").mkdir(parents=True, exist_ok=True)
        (root_mount / "etc/systemd/system/ssh.service.d/10-w103d-hostkeys.conf").write_text(
            "[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n")
        firstrun = root_mount / "etc/default/armbian-firstrun"
        if firstrun.is_file():
            firstrun.write_text(firstrun.read_text(errors="replace").replace(
                "OPENSSHD_REGENERATE_HOST_KEYS=true", "OPENSSHD_REGENERATE_HOST_KEYS=false"))
        # Boot artifacts come from the verified rebuilt system. Keep vendor U-Boot assets
        # and path convention from the actual reference, while replacing kernel/DTB/initrd.
        kernel_candidates = [p / f"vmlinuz-{release}" for p in source_boot_roots]
        initrd_candidates = [candidate for p in source_boot_roots
                             for candidate in (p / f"initrd.img-{release}", p / "uInitrd")]
        kernel_paths = list({p.resolve() for p in kernel_candidates if p.is_file()})
        if len(kernel_paths) != 1:
            raise SystemExit(f"expected one source boot kernel at exact release {release}; found {kernel_paths}")
        kernel_path = kernel_paths[0]
        initrd_path = next((p for p in initrd_candidates if p.is_file()), None)
        if not kernel_path.is_file() or not initrd_path:
            raise SystemExit("rebuilt rootfs is missing its exact release kernel/initramfs in /boot")
        if hash_file(kernel_path) != hashlib.sha256(kernel_image).hexdigest():
            raise SystemExit("rebuilt rootfs kernel differs from the exact locked Ophub kernel archive")
        boot_src = a.work / "boot-files"
        shutil.copytree(kernel_path.parent, boot_src, symlinks=False)
        (boot_src / "dtb-w103d").mkdir(parents=True, exist_ok=True)
        (boot_src / "dtb/amlogic").mkdir(parents=True, exist_ok=True)
        shutil.copy2(kernel_path, boot_src / "zImage")
        shutil.copy2(kernel_path, boot_src / "Image")
        (boot_src / "dtb-w103d/meson-g12a-w103d.dtb").write_bytes(dtb)
        (boot_src / "dtb/amlogic/meson-g12a-w103d.dtb").write_bytes(dtb)
        initrd_payload = ensure_uinitrd(kernel_path.parent, initrd_path, boot_src / "uInitrd")
        initrd_sha256 = hashlib.sha256(initrd_payload).hexdigest()
        if not a.ophub_boot_emmc.is_file():
            raise SystemExit("locked Ophub boot-emmc.cmd input is missing")
        boot_emmc_text = a.ophub_boot_emmc.read_text(errors="strict")
        expected_emmc = "booti ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}"
        if "${INITRD}" not in boot_emmc_text or expected_emmc not in boot_emmc_text:
            raise SystemExit("locked Ophub boot-emmc.cmd no longer has the reviewed eMMC boot chain")
        shutil.copy2(a.ophub_boot_emmc, boot_src / "boot-emmc.cmd")
        # The upstream eMMC script uses U-Boot's standard ramdisk_addr_r variable;
        # the separate vendor autoscript is the one whose updater expects initrd_addr.
        boot_cmd = boot_emmc_text
        (boot_src / "boot.cmd").write_text(boot_cmd)
        run("mkimage", "-A", "arm", "-T", "script", "-C", "none", "-n", "W103D Ophub eMMC boot",
            "-d", boot_src / "boot.cmd", boot_src / "boot.scr")
        # Keep vendor bootloader/logo and regenerate the vendor script from the audited
        # W103D command text so the binary payload and readable source stay in sync.
        ref_boot = a.work / "reference-boot.raw"
        from inspect_reference import sparse_to_raw
        sparse_to_raw(reference / "system.PARTITION", ref_boot)
        ref_boot_dir = a.work / "reference-boot"
        ref_boot_dir.mkdir()
        run("mcopy", "-s", "-i", ref_boot, "::*", str(ref_boot_dir) + "/")
        for name in ("u-boot.ext", "bootup.bmp", "emmc_autoscript.cmd"):
            if hash_file(ref_boot_dir / name) != layout["boot_files"].get(name):
                raise SystemExit(f"reference boot file changed from preflight: {name}")
            shutil.copy2(ref_boot_dir / name, boot_src / name)
        original_uenv = (ref_boot_dir / "uEnv.txt").read_text()
        if "root=LABEL=W103D_ROOT" not in original_uenv or "dtb-w103d/meson-g12a-w103d.dtb" not in original_uenv:
            raise SystemExit("preflight reference boot environment has unexpected root or DTB path")
        (boot_src / "uEnv.txt").write_text(
            "LINUX=/zImage\nINITRD=/uInitrd\nFDT=/dtb-w103d/meson-g12a-w103d.dtb\n"
            "APPEND=root=LABEL=W103D_ROOT rw rootwait rootfstype=ext4 console=ttyAML0,115200n8 console=tty0 net.ifnames=0 fsck.repair=yes\n")
        # W103D's chain script has two explicit paths: mainline U-Boot via u-boot.ext,
        # then the original vendor `booti` fallback using these exact FAT filenames.
        cmd = (ref_boot_dir / "emmc_autoscript.cmd").read_text()
        if "ramdisk-w103d.img" not in cmd or "dtb-w103d/meson-g12a-w103d.dtb" not in cmd:
            raise SystemExit("reference vendor boot script no longer names the measured W103D payload paths")
        cmd = re.sub(r"root=UUID=[0-9a-fA-F-]+", "root=LABEL=W103D_ROOT", cmd)
        cmd = cmd.replace("ramdisk-w103d.img", "uInitrd").replace("ramdisk_addr_r", "initrd_addr")
        if "setenv initrd_addr" not in cmd or "ramdisk_addr_r" in cmd or "booti " not in cmd:
            raise SystemExit("vendor fallback must explicitly define initrd_addr and use the audited booti path")
        (boot_src / "emmc_autoscript.cmd").write_text(cmd)
        run("mkimage", "-A", "arm", "-T", "script", "-C", "none", "-n", "W103D vendor fallback",
            "-d", boot_src / "emmc_autoscript.cmd", boot_src / "emmc_autoscript")
        run("mkimage", "-l", boot_src / "emmc_autoscript")
        for item in sorted(boot_src.iterdir()):
            run("mcopy", "-s", "-i", boot_raw, item, "::/")
        # First-boot SSH host keys and machine ID are regenerated on the device.
        ophub_release = root_mount / "etc/ophub-release"
        release_text = ophub_release.read_text(errors="replace")
        if "DISK_TYPE='usb'" in release_text:
            release_text = release_text.replace("DISK_TYPE='usb'", "DISK_TYPE='emmc'")
            ophub_release.write_text(release_text)
        if "DISK_TYPE='emmc'" not in ophub_release.read_text(errors="replace"):
            raise SystemExit("/etc/ophub-release did not accept DISK_TYPE=emmc")
        for generic in (root_mount / "etc/systemd/system/armbian-resize-filesystem.service",
                        root_mount / "etc/systemd/system/multi-user.target.wants/armbian-resize-filesystem.service"):
            if generic.is_symlink() or generic.is_file():
                generic.unlink()
        resize_script = root_mount / "usr/local/sbin/w103d-resize-rootfs"
        resize_script.parent.mkdir(parents=True, exist_ok=True)
        resize_script.write_text("#!/bin/sh\nset -eu\ntest ! -e /var/lib/w103d/rootfs-expanded || exit 0\nrootdev=$(findmnt -n -o SOURCE /)\ntest \"$(blkid -s LABEL -o value \"$rootdev\")\" = W103D_ROOT\nresize2fs \"$rootdev\"\nmkdir -p /var/lib/w103d\ntouch /var/lib/w103d/rootfs-expanded\n")
        resize_script.chmod(0o755)
        resize_service = root_mount / "etc/systemd/system/w103d-resize-rootfs.service"
        resize_service.write_text("[Unit]\nDescription=Expand W103D root filesystem\nAfter=local-fs.target\nConditionPathExists=!/var/lib/w103d/rootfs-expanded\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/w103d-resize-rootfs\nTimeoutStartSec=180\n[Install]\nWantedBy=multi-user.target\n")
        wants = root_mount / "etc/systemd/system/multi-user.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        (wants / "w103d-resize-rootfs.service").symlink_to("../w103d-resize-rootfs.service")
        dbus_id = root_mount / "var/lib/dbus/machine-id"
        dbus_id.unlink(missing_ok=True)
        dbus_id.parent.mkdir(parents=True, exist_ok=True)
        dbus_id.symlink_to("/etc/machine-id")
    finally:
        subprocess.run(["umount", str(root_mount)], check=True)
        unmount_all(loop, mounts)

    # Refuse any source partition capacity drift: the fixed approved payload sizes are
    # measured extents from the current ZIP preflight report, not raw component sizes.
    with boot_raw.open("rb") as stream:
        boot_sector = stream.read(512)
    if boot_sector[71:82].rstrip(b" ") != b"W103D_BOOT":
        raise SystemExit("boot FAT label readback failed")
    with root_raw.open("rb") as stream:
        stream.seek(1024)
        sb = stream.read(1024)
    if sb[56:58] != b"\x53\xef" or sb[120:136].split(b"\0", 1)[0] != b"W103D_ROOT":
        raise SystemExit("root ext4 superblock label/signature readback failed")
    run("e2fsck", "-fn", root_raw)
    sparse_stage = a.work / "sparse-stage"
    sparse_stage.mkdir()
    system_sparse = sparse_stage / "system.PARTITION"
    data_sparse = sparse_stage / "data.PARTITION"
    layout_report = {"system": raw_to_sparse(boot_raw, system_sparse), "data": raw_to_sparse(root_raw, data_sparse)}
    from inspect_reference import sparse_to_raw
    for raw, sparse in ((boot_raw, system_sparse), (root_raw, data_sparse)):
        roundtrip = a.work / f"{raw.stem}-roundtrip.raw"
        sparse_to_raw(sparse, roundtrip)
        if hash_file(roundtrip) != hash_file(raw):
            raise SystemExit(f"Android sparse roundtrip mismatch for {raw.name}")
        roundtrip.unlink()
    shutil.copyfile(system_sparse, reference / "system.PARTITION")
    shutil.copyfile(data_sparse, reference / "data.PARTITION")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    run(a.packer, "-r", reference / "image.cfg", reference, a.output)
    run(a.packer, "-c", a.output)
    report = {
        "reference_zip_image_sha256": json.loads(Path("config/reference.lock.json").read_text())["image_sha256"],
        "reference_component_sha256": {name: hash_file(reference / name) for name in VENDOR_COMPONENTS},
        "system_input_sha256": hash_file(a.system_image), "kernel_archive_sha256": hash_file(a.kernel_archive),
        "kernel_release": release, "layout": {"boot_bytes": boot_raw.stat().st_size, "root_bytes": root_raw.stat().st_size,
            "boot_label": "W103D_BOOT", "root_label": "W103D_ROOT", "root_uuid": "01b94932-6a4a-4c81-9a71-20bd55b675a8"},
        "armbian_version": armbian_version,
        "armbian_release": armbian_fields,
        "sparse_roundtrip": layout_report,
        "boot_files_sha256": {p.relative_to(boot_src).as_posix(): hash_file(p) for p in boot_src.rglob("*") if p.is_file()},
        "boot_initrd_sha256": initrd_sha256,
        "initrd_payload_sha256": initrd_sha256,
        "kernel_config_sha256": hashlib.sha256(kernel_config.encode()).hexdigest(),
        "kernel_config_options": config_options,
        "source_lock": source_lock,
        "source_lock_sha256": hash_file(a.sources_lock),
        "base_image_lock": base_image_lock,
        "base_image_lock_sha256": hash_file(a.base_image_lock),
        "ophub_boot_emmc_cmd_sha256": hash_file(a.ophub_boot_emmc),
        "boot_chain": {
            "boot_cmd_source": "ophub/amlogic-s9xxx-armbian/build-armbian/armbian-files/platform-files/amlogic/bootfs/boot-emmc.cmd",
            "boot_cmd_source_sha256": hash_file(a.ophub_boot_emmc),
            "runtime_initrd": "uInitrd",
            "vendor_boot_script_legacy_header": True,
        },
        "output": {"name": a.output.name, "bytes": a.output.stat().st_size, "sha256": hash_file(a.output)},
        "hardware_flashed": False,
    }
    (a.work / "assembly-input-report.json").write_text(json.dumps(report, indent=2) + "\n")
    a.output.with_suffix(".sha256").write_text(f"{hash_file(a.output)}  {a.output.name}\n")
    shutil.copy2(a.work / "assembly-input-report.json", a.output.with_suffix(".report.json"))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
