#!/usr/bin/env python3
"""Lock the newest generic Trixie arm64 server image published by Ophub."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

API = "https://api.github.com"
REPOSITORY = "ophub/amlogic-s9xxx-armbian"
RELEASE_TAG = re.compile(r"^Armbian_trixie_arm64_server_\d{4}\.\d{2}$")
ASSET_NAME = re.compile(r"^Armbian_(\d+\.\d+\.\d+)-trunk_trixie_arm64_(\d+\.\d+\.\d+)\.img\.gz$")


def get_json(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "w103d-base-image-lock/1.0",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=45) as response:
        return json.load(response)


def main() -> int:
    releases = get_json(f"{API}/repos/{REPOSITORY}/releases?per_page=100")
    if not isinstance(releases, list):
        raise SystemExit(f"unexpected Ophub releases API response: expected array, got {type(releases).__name__}")
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease") or not RELEASE_TAG.fullmatch(tag):
            continue
        for asset in release.get("assets", []):
            match = ASSET_NAME.fullmatch(asset.get("name", ""))
            if not match:
                continue
            version = tuple(int(part) for part in match.group(1).split("."))
            candidates.append((version, asset.get("updated_at", ""), release, asset, match.groups()))
    if not candidates:
        raise SystemExit(f"no Ophub {REPOSITORY} Trixie arm64 server release contains a generic -trunk IMG.GZ with API SHA-256")
    _, updated, release, asset, groups = max(candidates, key=lambda row: (row[0], row[1]))
    armbian_version, kernel_track = groups
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"newest Ophub Armbian {armbian_version} asset has no immutable SHA-256 digest; refusing downgrade: {release['tag_name']}/{asset.get('name')}: {digest!r}")
    result = {
        "source": "ophub/amlogic-s9xxx-armbian official generic Trixie arm64 server release; rebuilt for W103D with the separately locked Ophub kernel archive",
        "repository": REPOSITORY,
        "release_tag": release["tag_name"],
        "release_id": release["id"],
        "release_published_at": release.get("published_at"),
        "asset_id": asset["id"],
        "filename": asset["name"],
        "url": asset["url"],
        "browser_download_url": asset["browser_download_url"],
        "asset_size": asset["size"],
        "asset_updated_at": updated,
        "asset_digest": digest,
        "sha256": digest.removeprefix("sha256:"),
        "armbian_version": armbian_version,
        "upstream_kernel_track": kernel_track,
        "download_accept": "application/octet-stream",
    }
    out_path = Path(os.environ.get("BASE_IMAGE_LOCK_OUT", "base-image.lock.json"))
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
