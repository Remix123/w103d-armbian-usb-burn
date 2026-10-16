#!/usr/bin/env python3
"""Inventory and hash the exact Ophub kernel debs produced by the build."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("legacy_deb_dir", nargs="?", type=Path)
    ap.add_argument("--deb-dir", type=Path)
    ap.add_argument("--version", default=os.environ.get("KERNEL_VERSION"))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--runtime-archive", type=Path)
    args = ap.parse_args()
    if bool(args.deb_dir) == bool(args.legacy_deb_dir):
        ap.error("provide either --deb-dir or the legacy positional directory")
    args.deb_dir = args.deb_dir or args.legacy_deb_dir
    args.out = args.out or args.deb_dir / "kernel-manifest.json"
    if not args.version or not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        ap.error("provide a strict --version (or KERNEL_VERSION for the legacy caller)")
    packages = []
    for path in sorted(args.deb_dir.glob("*.deb")):
        fields = {}
        for field in ("Package", "Version", "Architecture"):
            fields[field] = subprocess.run(["dpkg-deb", "-f", str(path), field], check=True,
                                           text=True, stdout=subprocess.PIPE).stdout.strip()
        package, version, arch = fields["Package"], fields["Version"], fields["Architecture"]
        packages.append({"file": path.name, "package": package, "version": version, "architecture": arch,
                         "size": path.stat().st_size, "sha256": sha(path)})
    if not packages:
        raise SystemExit("no kernel deb packages were produced")
    images = [p for p in packages if p["package"].startswith("linux-image") and p["version"] == f"{args.version}-1-ophub"]
    dtbs = [p for p in packages if p["package"].startswith("linux-dtb-") and p["version"] == f"{args.version}-1-ophub"]
    if len(images) != 1 or not dtbs:
        raise SystemExit(f"expected one linux-image and at least one linux-dtb package; found images={len(images)} dtbs={len(dtbs)}")
    if images[0]["architecture"] != "arm64" or any(p["architecture"] != "arm64" for p in dtbs):
        raise SystemExit("kernel image or DTB package architecture mismatch")
    doc = {"kernel_release": f"{args.version}-ophub", "packages": packages,
           "image_package": images[0]["package"], "dtb_packages": [p["package"] for p in dtbs],
           "hardware_tested": False}
    if any(p["version"] != f"{args.version}-1-ophub" or p["architecture"] != "arm64" for p in packages):
        raise SystemExit("kernel package version or architecture mismatch")
    if "linux-dtb-meson64" not in doc["dtb_packages"]:
        raise SystemExit("kernel packages lack the W103D meson64 DTB package")
    if args.runtime_archive:
        doc["runtime_archive"] = {"file": args.runtime_archive.name,
                                  "size": args.runtime_archive.stat().st_size,
                                  "sha256": sha(args.runtime_archive)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
