#!/usr/bin/env python3
"""Resolve current Ophub W103D sources once and emit immutable checkout refs."""
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


def ref(owner: str, repo: str, branch: str) -> str:
    return get_json(f"{API}/repos/{owner}/{repo}/commits/{branch}")["sha"]


def main() -> int:
    ophub_repo = "ophub/amlogic-s9xxx-armbian"
    ophub_sha = ref("ophub", "amlogic-s9xxx-armbian", "main")
    model_url = f"https://raw.githubusercontent.com/{ophub_repo}/{ophub_sha}/build-armbian/armbian-files/common-files/etc/model_database.conf"
    rows = []
    for line in get_text(model_url).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [x.strip() for x in line.split(":")]
        if len(fields) >= 16 and fields[14] == "s905l3a-w103d" and fields[15].lower() == "yes":
            rows.append(fields)
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one enabled W103D model row, found {len(rows)}")
    model = rows[0]
    series_field = model[9]
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

    release = get_json(f"{API}/repos/{ophub_repo}/releases/latest")
    candidates = []
    image_re = re.compile(r"^Armbian_(\d+\.\d+\.\d+)_amlogic_s905l3a-w103d_([a-z]+)_(\d+\.\d+\.\d+)_server_(\d{4}\.\d{2}\.\d{2})\.img\.gz$")
    for asset in release.get("assets", []):
        m = image_re.fullmatch(asset["name"])
        if m:
            candidates.append((m.groups(), asset))
    if not candidates:
        raise SystemExit(f"latest release {release['tag_name']} has no W103D server image")
    candidates.sort(key=lambda x: (x[0][3], tuple(map(int, x[0][2].split("."))), x[0][0]))
    image_match, base_asset = candidates[-1]
    if base_asset.get("size", 0) <= 0 or not base_asset.get("browser_download_url"):
        raise SystemExit("selected official base image has invalid asset metadata")

    result = {
        "resolution_time_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "ophub": {"repository": ophub_repo, "commit": ophub_sha, "model_database_url": model_url,
                  "model": model[1], "soc": model[2], "linux_dtb": model[3],
                  "kernel_tags": model[9], "board": model[14]},
        "kernel": {"repository": kernel_repo, "commit": kernel_sha, "branch": "main", "version": kernel_version,
                   "checkout_path": f"compile-kernel/linux-{kernel_series}"},
        "base_image": {"repository": ophub_repo, "release_tag": release["tag_name"],
                       "asset_name": base_asset["name"], "asset_id": base_asset["id"],
                       "size": base_asset["size"], "download_url": base_asset["browser_download_url"],
                       "sha256": base_asset.get("digest")},
        "container": {"image": "ophub/armbian-resolute:arm64", "digest": None},
        "limitations": ["GitHub release metadata may not publish a base-image digest; workflow must hash the downloaded bytes before use.",
                        "Container digest is resolved separately before compilation and must be passed to recompile."]
    }
    out = Path(os.environ.get("SOURCE_LOCK_OUT", "sources.lock.json"))
    out.write_text(json.dumps(result, indent=2) + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            for key, value in {"ophub_sha": ophub_sha, "kernel_sha": kernel_sha, "kernel_series": kernel_series,
                               "kernel_version": kernel_version, "base_asset": base_asset["name"],
                               "base_url": base_asset["browser_download_url"], "base_size": base_asset["size"]}.items():
                f.write(f"{key}={value}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
