#!/usr/bin/env python3
"""Assemble W103D USB burning image from latest Ophub base + compiled kernel.

Simplified version of assemble_usb_image.py for rolling/latest builds.
Uses 2021 Wi-Fi firmware from repository (config/wifi-firmware.lock.json).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def run(*argv: str | Path, capture: bool = True) -> str:
    result = subprocess.run(
        [str(x) for x in argv],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None
    )
    return result.stdout.strip() if capture else ""


def hash_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def install_wifi_firmware(root_mount: Path, wifi_config: dict) -> dict:
    """Install 2021 Wi-Fi firmware from repository into rootfs."""
    install_dir = root_mount / wifi_config["install_dir"]
    install_dir.mkdir(parents=True, exist_ok=True)

    installed = []
    for fw in wifi_config["files"]:
        repo_path = Path(fw["repo_path"])
        if not repo_path.is_file():
            raise FileNotFoundError(f"Wi-Fi firmware not found: {repo_path}")

        actual_sha = hash_file(repo_path)
        if actual_sha != fw["sha256"]:
            raise ValueError(
                f"Wi-Fi firmware SHA256 mismatch for {fw['name']}: "
                f"expected {fw['sha256']}, got {actual_sha}"
            )

        target = install_dir / fw["name"]
        shutil.copy2(repo_path, target)
        print(f"Installed Wi-Fi firmware: {fw['name']} ({fw['version']})")

        installed.append({
            "name": fw["name"],
            "sha256": actual_sha,
            "version": fw["version"],
            "source": fw["source"]
        })

    return {"firmware": installed, "install_dir": str(install_dir.relative_to(root_mount))}


def extract_kernel_from_debs(kernel_debs_dir: Path, work_dir: Path) -> dict:
    """Extract kernel components from .deb packages."""
    boot_dir = work_dir / "boot"
    boot_dir.mkdir(parents=True, exist_ok=True)

    # Install debs to extract files
    for deb in sorted(kernel_debs_dir.glob("*.deb")):
        print(f"Extracting {deb.name}...")
        run("dpkg-deb", "-x", deb, work_dir)

    # Find kernel files
    kernel_version = None
    vmlinuz = None
    config = None

    for candidate in boot_dir.glob("vmlinuz-*"):
        if not kernel_version:
            kernel_version = candidate.name.replace("vmlinuz-", "")
            vmlinuz = candidate
            config = boot_dir / f"config-{kernel_version}"
            break

    if not vmlinuz or not config.exists():
        raise FileNotFoundError("Could not find kernel image or config in debs")

    # Find DTB
    dtb_dir = work_dir / "boot" / "dtb" / "amlogic"
    if not dtb_dir.exists():
        dtb_dir = work_dir / "usr" / "lib" / "linux-image" / kernel_version / "amlogic"

    dtb = dtb_dir / "meson-g12a-w103d.dtb"
    if not dtb.exists():
        raise FileNotFoundError(f"W103D DTB not found in {dtb_dir}")

    # Find modules
    modules_dir = work_dir / "lib" / "modules" / kernel_version
    if not modules_dir.exists():
        raise FileNotFoundError(f"Kernel modules not found: {modules_dir}")

    return {
        "version": kernel_version,
        "vmlinuz": vmlinuz,
        "config": config,
        "dtb": dtb,
        "modules_dir": modules_dir
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-image", type=Path, required=True,
                    help="Ophub Armbian base image (.img)")
    ap.add_argument("--kernel-archive", type=Path, required=True,
                    help="Compiled kernel archive (.tar.xz with .deb files)")
    ap.add_argument("--kernel-version", required=True,
                    help="Kernel version string")
    ap.add_argument("--ophub-boot-cmd", type=Path, required=True,
                    help="Ophub boot-emmc.cmd source")
    ap.add_argument("--wifi-firmware-config", type=Path, required=True,
                    help="config/wifi-firmware.lock.json")
    ap.add_argument("--output-template", required=True,
                    help="Output filename template with {armbian_version} and {kernel_version}")
    args = ap.parse_args()

    if not args.base_image.exists():
        raise FileNotFoundError(f"Base image not found: {args.base_image}")
    if not args.kernel_archive.exists():
        raise FileNotFoundError(f"Kernel archive not found: {args.kernel_archive}")

    # Load Wi-Fi firmware config
    with open(args.wifi_firmware_config) as f:
        wifi_config = json.load(f)

    if wifi_config["source"] != "repository":
        raise ValueError("wifi-firmware.lock.json must have source='repository'")

    print(f"=== Assembling W103D Latest Ophub USB Image ===")
    print(f"Base image: {args.base_image}")
    print(f"Kernel: {args.kernel_version}")

    with tempfile.TemporaryDirectory() as tmpdir:
        work = Path(tmpdir)

        # Extract kernel debs
        print("\n[1/6] Extracting kernel packages...")
        kernel_debs = work / "kernel-debs"
        kernel_debs.mkdir()
        with tarfile.open(args.kernel_archive) as tar:
            tar.extractall(kernel_debs)

        kernel_components = extract_kernel_from_debs(kernel_debs, work / "kernel-extracted")

        # Make working copy of base image
        print("\n[2/6] Preparing base image...")
        work_image = work / "working.img"
        shutil.copy2(args.base_image, work_image)

        # Mount image partitions
        print("\n[3/6] Mounting image...")
        loop_dev = run("losetup", "--show", "-f", "-P", work_image)
        print(f"Loop device: {loop_dev}")

        try:
            boot_part = Path(f"{loop_dev}p1")
            root_part = Path(f"{loop_dev}p2")

            # Wait for partitions
            run("partprobe", loop_dev)
            run("udevadm", "settle")

            boot_mount = work / "boot"
            root_mount = work / "root"
            boot_mount.mkdir()
            root_mount.mkdir()

            run("mount", boot_part, boot_mount)
            run("mount", root_part, root_mount)

            try:
                # Install kernel
                print("\n[4/6] Installing kernel...")

                # Copy kernel image
                (boot_mount / f"vmlinuz-{kernel_components['version']}").write_bytes(
                    kernel_components['vmlinuz'].read_bytes()
                )

                # Copy DTB
                (boot_mount / "dtb" / "amlogic" / "meson-g12a-w103d.dtb").write_bytes(
                    kernel_components['dtb'].read_bytes()
                )

                # Install modules
                modules_target = root_mount / "lib" / "modules"
                if (modules_target / kernel_components['version']).exists():
                    shutil.rmtree(modules_target / kernel_components['version'])
                shutil.copytree(
                    kernel_components['modules_dir'],
                    modules_target / kernel_components['version'],
                    symlinks=True
                )

                # Update boot configuration
                print("\n[5/6] Updating boot configuration...")
                uenv = boot_mount / "uEnv.txt"
                uenv_text = uenv.read_text() if uenv.exists() else ""

                # Update FDT reference
                uenv_text = re.sub(
                    r'FDT=/dtb/amlogic/.*\.dtb',
                    'FDT=/dtb/amlogic/meson-g12a-w103d.dtb',
                    uenv_text
                )

                # Update kernel reference
                uenv_text = re.sub(
                    r'LINUX=/vmlinuz-.*',
                    f'LINUX=/vmlinuz-{kernel_components["version"]}',
                    uenv_text
                )

                uenv.write_text(uenv_text)

                # Install boot-emmc.cmd
                shutil.copy2(args.ophub_boot_cmd, boot_mount / "boot-emmc.cmd")

                # Install Wi-Fi firmware
                print("\n[6/6] Installing Wi-Fi firmware (2021 drivers)...")
                wifi_manifest = install_wifi_firmware(root_mount, wifi_config)

                # Update /etc/ophub-release
                ophub_release = root_mount / "etc" / "ophub-release"
                if ophub_release.exists():
                    release_text = ophub_release.read_text()
                    if "DISK_TYPE=" not in release_text:
                        release_text += "\nDISK_TYPE='emmc'\n"
                    else:
                        release_text = re.sub(
                            r"DISK_TYPE='[^']*'",
                            "DISK_TYPE='emmc'",
                            release_text
                        )
                    ophub_release.write_text(release_text)

                print("\n✓ Image assembly complete")

            finally:
                # Unmount
                run("umount", boot_mount)
                run("umount", root_mount)

        finally:
            run("losetup", "-d", loop_dev)

        # Determine Armbian version from base
        print("\nExtracting Armbian version...")
        armbian_version = "unknown"
        try:
            # Try to extract from base image filename or mount
            # For simplicity, use "Latest" as version string
            armbian_version = "Latest"
        except Exception:
            pass

        # Move to final location
        output = Path(args.output_template.format(
            armbian_version=armbian_version,
            kernel_version=args.kernel_version
        ))

        shutil.move(work_image, output)
        print(f"\n✓ Final image: {output}")
        print(f"  Size: {output.stat().st_size / 1024**2:.1f} MB")
        print(f"  SHA256: {hash_file(output)}")

        # Create manifest
        manifest = {
            "image": output.name,
            "image_sha256": hash_file(output),
            "kernel_version": args.kernel_version,
            "armbian_version": armbian_version,
            "build_type": "latest-ophub",
            "wifi_firmware": wifi_manifest
        }

        manifest_path = output.parent / f"{output.stem}.manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        print(f"✓ Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
