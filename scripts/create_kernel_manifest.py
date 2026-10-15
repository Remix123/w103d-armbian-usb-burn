#!/usr/bin/env python3
"""Create kernel manifest for latest-ophub build."""
import json
import hashlib
import glob
import os
import sys

if len(sys.argv) != 2:
    print("Usage: create_kernel_manifest.py <kernel_debs_dir>")
    sys.exit(1)

os.chdir(sys.argv[1])

debs = sorted(glob.glob("*.deb"))
manifest = {
    "ophub_sha": os.environ["OPHUB_SHA"],
    "kernel_sha": os.environ["KERNEL_SHA"],
    "kernel_series": os.environ["KERNEL_SERIES"],
    "kernel_version": os.environ["KERNEL_VERSION"],
    "build_type": "latest-ophub",
    "packages": []
}

for deb in debs:
    with open(deb, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    manifest["packages"].append({
        "name": deb,
        "sha256": sha,
        "size": os.path.getsize(deb)
    })

with open("kernel-manifest.json", "w") as f:
    json.dump(manifest, f, indent=2)

print(json.dumps(manifest, indent=2))
