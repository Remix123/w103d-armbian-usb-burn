#!/usr/bin/env python3
"""Resolve current Ophub W103D kernel sources and build tools into immutable refs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

API = "https://api.github.com"


def get_json(url: str):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def get_text(url: str) -> str:
    with urlopen(Request(url, headers={"User-Agent": "w103d-build-source-lock"}), timeout=30) as response:
        return response.read().decode("utf-8")


def docker_manifest_digest(image: str, tag: str) -> str:
    token_url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:" + image + ":pull"
    with urlopen(Request(token_url), timeout=30) as response:
        token = json.load(response)["token"]
    accept = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))
    request = Request(f"https://registry-1.docker.io/v2/{image}/manifests/{tag}",
                      headers={"Authorization": f"Bearer {token}", "Accept": accept})
    with urlopen(request, timeout=60) as response:
        digest = response.headers.get("Docker-Content-Digest")
        if not digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SystemExit("Docker registry did not return an immutable manifest digest")
        return digest


def ref(owner: str, repo: str, branch: str) -> str:
    return get_json(f"{API}/repos/{owner}/{repo}/commits/{branch}")["sha"]


def enabled_w103d_row(model_database: str) -> list[str]:
    rows = []
    for line in model_database.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [part.strip() for part in line.split(":")]
        # model_database.conf documents 15 colon-delimited columns; indices
        # below are zero-based (kernel tags=8, board=13, build=14).
        if len(fields) >= 15 and fields[13] == "s905l3a-w103d" and fields[14].lower() == "yes":
            rows.append(fields)
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one enabled W103D model row, found {len(rows)}")
    return rows[0]


def main() -> int:
    ophub_repo = "ophub/amlogic-s9xxx-armbian"
    ophub_sha = ref("ophub", "amlogic-s9xxx-armbian", "main")
    model_url = f"https://raw.githubusercontent.com/{ophub_repo}/{ophub_sha}/build-armbian/armbian-files/common-files/etc/model_database.conf"
    model = enabled_w103d_row(get_text(model_url))
    series_field = model[8]
    match = re.fullmatch(r"stable/(\d+\.\d+\.y)", series_field)
    if not match:
        raise SystemExit(f"unexpected W103D kernel series: {series_field}")
    kernel_series = match.group(1)
    kernel_repo = f"ophub/linux-{kernel_series}"
    kernel_sha = ref("ophub", f"linux-{kernel_series}", "main")
    makefile_url = f"https://raw.githubusercontent.com/{kernel_repo}/{kernel_sha}/Makefile"
    makefile = get_text(makefile_url)
    parts = []
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        m = re.search(rf"^{key}\s*=\s*(\d+)\s*$", makefile, re.M)
        if not m:
            raise SystemExit(f"missing {key} in pinned kernel Makefile")
        parts.append(m.group(1))
    kernel_version = ".".join(parts)
    kernel_meta_sha = ref("ophub", "kernel", "main")
    config_path = f"kernel-config/release/stable/config-{'.'.join(parts[:2])}"
    config_url = f"https://raw.githubusercontent.com/ophub/kernel/{kernel_meta_sha}/{config_path}"
    config_text = get_text(config_url)
    config_sha = __import__("hashlib").sha256(config_text.encode()).hexdigest()

    container_name = "ophub/armbian-resolute"
    container_digest = docker_manifest_digest(container_name, "arm64")
    toolchain_release = get_json(f"{API}/repos/ophub/kernel/releases/tags/dev")
    toolchain_name = "arm-gnu-toolchain-15.3.rel1-aarch64-aarch64-none-linux-gnu.tar.xz"
    toolchain_matches = [a for a in toolchain_release.get("assets", []) if a["name"] == toolchain_name]
    if len(toolchain_matches) != 1:
        raise SystemExit("Ophub kernel dev release does not contain exactly one pinned GNU toolchain asset")
    toolchain = toolchain_matches[0]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", toolchain.get("digest", "")):
        raise SystemExit("GitHub API did not provide an immutable SHA-256 for the GNU toolchain asset")

    result = {
        "resolution_time_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "ophub": {"repository": ophub_repo, "commit": ophub_sha, "model_database_url": model_url,
                  "model": model[1], "soc": model[2], "linux_dtb": model[3],
                  "kernel_tags": model[8], "board": model[13]},
        "kernel": {"repository": kernel_repo, "commit": kernel_sha, "branch": "main", "version": kernel_version,
                   "checkout_path": f"compile-kernel/kernel/linux-{kernel_series}-main",
                   "config_repository": "ophub/kernel", "config_commit": kernel_meta_sha,
                   "config_path": config_path, "config_sha256": config_sha},
        "kernel_toolchain": {"repository": "ophub/kernel", "release_tag": toolchain_release["tag_name"],
                             "asset_name": toolchain_name, "size": toolchain["size"],
                             "download_url": toolchain["browser_download_url"],
                             "sha256": toolchain["digest"].removeprefix("sha256:")},
        "container": {"image": f"{container_name}:arm64", "digest": container_digest},
        "limitations": ["The kernel build uses exact Git commit IDs and the GitHub-reported SHA-256 for the GNU toolchain asset.",
                        "The compiler container is invoked with the exact registry digest, not the mutable arm64 tag."]
    }
    out = Path(os.environ.get("SOURCE_LOCK_OUT", "sources.lock.json"))
    out.write_text(json.dumps(result, indent=2) + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            for key, value in {"ophub_sha": ophub_sha, "kernel_sha": kernel_sha, "kernel_series": kernel_series,
                               "kernel_meta_sha": kernel_meta_sha, "kernel_version": kernel_version,
                               "kernel_config": config_path, "kernel_config_sha256": config_sha,
                               "container_digest": container_digest, "toolchain_asset": toolchain_name,
                               "toolchain_url": toolchain["browser_download_url"],
                               "toolchain_sha256": toolchain["digest"].removeprefix("sha256:")}.items():
                f.write(f"{key}={value}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
