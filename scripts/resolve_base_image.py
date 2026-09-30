#!/usr/bin/env python3
"""Resolve the newest official generic Odroid N2 Trixie minimal image + checksum."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

ARCHIVE = "https://dl.armbian.com/odroidn2/archive/"
PATTERN = re.compile(r"Armbian_(\d+)\.(\d+)\.(\d+)_Odroidn2_trixie_current_(\d+)\.(\d+)\.(\d+)_minimal\.img\.xz")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "w103d-armbian-build/1.0"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    parser = Links()
    parser.feed(fetch(ARCHIVE).decode("utf-8", errors="replace"))
    candidates = []
    for href in parser.hrefs:
        name = href.rsplit("/", 1)[-1]
        match = PATTERN.fullmatch(name)
        if match:
            values = tuple(int(v) for v in match.groups())
            candidates.append((values, name))
    if not candidates:
        raise SystemExit("official Armbian archive has no Trixie Odroid N2 current minimal image")
    _, name = max(candidates)
    url = ARCHIVE + name
    sha_text = fetch(url + ".sha").decode("utf-8", errors="strict").strip()
    found = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", sha_text)
    if not found or found.group(2).strip() not in (name, ""):
        raise SystemExit(f"unexpected SHA sidecar format for {name}: {sha_text!r}")
    result = {"source": "official Armbian Odroid N2 generic Trixie minimal image; rebuilt by locked Ophub action",
              "archive_index": ARCHIVE, "filename": name, "url": url,
              "sha256": found.group(1).lower(), "sha256_sidecar": url + ".sha"}
    out_path = Path(os.environ.get("BASE_IMAGE_LOCK_OUT", "base-image.lock.json"))
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
