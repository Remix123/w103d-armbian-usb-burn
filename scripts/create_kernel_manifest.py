#!/usr/bin/env python3
"""Inventory and hash the exact Ophub kernel debs produced by the build."""
import argparse
import hashlib
import json
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
    ap.add_argument("--deb-dir", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
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
    images = [p for p in packages if p["package"].startswith("linux-image") and args.version in p["version"]]
    dtbs = [p for p in packages if p["package"].startswith("linux-dtb-") and args.version in p["version"]]
    if len(images) != 1 or not dtbs:
        raise SystemExit(f"expected one linux-image and at least one linux-dtb package; found images={len(images)} dtbs={len(dtbs)}")
    if images[0]["architecture"] != "arm64" or any(p["architecture"] != "arm64" for p in dtbs):
        raise SystemExit("kernel image or DTB package architecture mismatch")
    doc = {"kernel_release": f"{args.version}-ophub", "packages": packages,
           "image_package": images[0]["package"], "dtb_packages": [p["package"] for p in dtbs],
           "hardware_tested": False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
