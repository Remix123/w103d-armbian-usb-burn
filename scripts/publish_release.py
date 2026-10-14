#!/usr/bin/env python3
"""Publish the verified USB Burning Tool ZIP as the single latest GitHub Release.

Runs after orchestrate_build.py. Downloads the exact ZIP artifact recorded in the
pipeline state, re-verifies it (ZIP SHA-256, internal SHA256SUMS, IMG SHA-256),
creates a release, verifies the uploaded asset digests, and only then deletes
older delivery releases (tag prefix ``w103d-usb-``) together with their tags.
The kernel feed (``kernel_stable``) and reference input releases are never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deliver_image_zip import verify_artifact_download  # noqa: E402

TAG_PREFIX = "w103d-usb-"
PROTECTED_TAGS = {"kernel_stable"}


def gh(*args: str, capture: bool = True) -> str:
    result = subprocess.run(["gh", *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    return result.stdout if capture else ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def release_tag(image_name: str, head_sha: str) -> str:
    match = re.fullmatch(r"W103D_Armbian_(\d+\.\d+\.\d+)_(\d+\.\d+\.\d+)_USB_Burning_Tool\.img", image_name)
    if not match:
        raise SystemExit(f"unexpected image basename: {image_name}")
    return f"{TAG_PREFIX}{match.group(1)}-{match.group(2)}-{head_sha[:7]}"


def read_member_json(archive: Path, basename: str) -> dict:
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if PurePosixPath(n).name == basename]
        if not names:
            return {}
        return json.loads(zf.read(sorted(names, key=len)[0]))


def release_notes(state: dict, result: dict, archive: Path, repo: str) -> str:
    lock = read_member_json(archive, "sources.lock.json")
    base = read_member_json(archive, "base-image.lock.json")
    report = read_member_json(archive, "verification-report.json")
    ophub, kernel = lock.get("ophub", {}), lock.get("kernel", {})
    lines = [
        f"W103D Armbian {report.get('armbian_version', '?')} (Trixie server), kernel {report.get('kernel_release', '?')}, "
        "image for the Amlogic USB Burning Tool.",
        "",
        "Unzip, check `SHA256SUMS`, then load the `.img` in USB Burning Tool.",
        "",
        "| | |",
        "|---|---|",
        f"| ZIP | `{result['zip_name']}` |",
        f"| ZIP SHA-256 | `{result['zip_sha256']}` |",
        f"| IMG | `{result['image_name']}` |",
        f"| IMG SHA-256 | `{result['image_sha256']}` |",
        f"| Base image | `{base.get('filename', '?')}` ({base.get('release_tag', '?')}) |",
        f"| ophub/amlogic-s9xxx-armbian | `{ophub.get('commit', '?')}` |",
        f"| {kernel.get('repository', 'kernel')} | `{kernel.get('commit', '?')}` ({kernel.get('version', '?')}) |",
        f"| Kernel config | `{kernel.get('config_path', '?')}` @ `{kernel.get('config_commit', '?')}` |",
    ]
    for patch in lock.get("kernel_patches", []):
        lines.append(f"| Kernel patch | `{patch['path']}` `{patch['sha256']}` |")
    for fw in report.get("wifi_firmware", []):
        lines.append(f"| Wi-Fi firmware | `{fw['name']}` {fw['version']} `{fw['sha256']}` |")
    lines += [
        f"| Workflow source | `{state['ref']}` @ `{state['head_sha']}` |",
        f"| Pipeline run | https://github.com/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '?')} |",
        "",
        "Only the newest build is kept as a release; older ones are deleted automatically.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    temp = Path(os.environ["RUNNER_TEMP"])
    state_path = temp / "w103d-pipeline-state.json"
    state = json.loads(state_path.read_text())
    result = state.get("result") or {}
    for key in ("zip_artifact_id", "zip_sidecar_artifact_id", "zip_name", "zip_sha256", "image_name", "image_sha256"):
        if not result.get(key):
            raise SystemExit(f"pipeline state has no verified result field {key}; refusing to publish")
    work = temp / "release"
    work.mkdir(exist_ok=True)

    transfer = work / "zip-artifact-transfer.bin"
    transfer.write_bytes(subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{result['zip_artifact_id']}/zip"],
                                        check=True, stdout=subprocess.PIPE).stdout)
    archive = verify_artifact_download(transfer, work / "asset", result["zip_name"], result["zip_sha256"],
                                       result["image_name"], result["image_sha256"])
    transfer.unlink()
    sidecar_transfer = work / "sidecar-transfer.bin"
    sidecar_transfer.write_bytes(subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{result['zip_sidecar_artifact_id']}/zip"],
                                                check=True, stdout=subprocess.PIPE).stdout)
    sidecar = work / "asset" / f"{result['zip_name']}.sha256"
    if zipfile.is_zipfile(sidecar_transfer):
        with zipfile.ZipFile(sidecar_transfer) as zf:
            members = [n for n in zf.namelist() if PurePosixPath(n).name == sidecar.name]
            if len(members) != 1:
                raise SystemExit(f"sidecar artifact does not contain one {sidecar.name}")
            sidecar.write_bytes(zf.read(members[0]))
    else:
        sidecar.write_bytes(sidecar_transfer.read_bytes())
    if sidecar.read_text().split() != [result["zip_sha256"], result["zip_name"]]:
        raise SystemExit("SHA-256 sidecar does not name the verified ZIP digest and basename")

    tag = release_tag(result["image_name"], state["head_sha"])
    notes = work / "notes.md"
    notes.write_text(release_notes(state, result, archive, repo))
    title = result["zip_name"].removesuffix(".zip")

    existing = json.loads(gh("api", f"repos/{repo}/releases?per_page=100"))
    if any(r["tag_name"] == tag for r in existing):
        # Same source commit and versions rebuilt: replace that release in place.
        gh("release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag")
    gh("release", "create", tag, str(archive), str(sidecar), "--repo", repo, "--target", state["head_sha"],
       "--title", title, "--notes-file", str(notes), "--latest")

    created = json.loads(gh("api", f"repos/{repo}/releases/tags/{tag}"))
    assets = {a["name"]: a for a in created.get("assets", [])}
    for path in (archive, sidecar):
        asset = assets.get(path.name)
        if not asset or asset.get("size") != path.stat().st_size:
            raise SystemExit(f"release asset {path.name} is missing or has the wrong size")
        digest = asset.get("digest")
        if digest and digest != f"sha256:{sha256(path)}":
            raise SystemExit(f"release asset {path.name} digest {digest} differs from the verified file")
        if not digest:
            check = work / "readback"
            check.mkdir(exist_ok=True)
            gh("release", "download", tag, "--repo", repo, "--pattern", path.name, "--dir", str(check), "--clobber")
            if sha256(check / path.name) != sha256(path):
                raise SystemExit(f"downloaded release asset {path.name} differs from the verified file")

    removed = []
    for release in json.loads(gh("api", f"repos/{repo}/releases?per_page=100")):
        old = release["tag_name"]
        if old == tag or old in PROTECTED_TAGS or not old.startswith(TAG_PREFIX):
            continue
        gh("release", "delete", old, "--repo", repo, "--yes", "--cleanup-tag")
        removed.append(old)

    url = created["html_url"]
    state["release"] = {"tag": tag, "url": url, "removed_previous": removed}
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"\n## Release\n\n- [{tag}]({url}) (latest)\n- Removed older releases: {', '.join(removed) or 'none'}\n")
    print(json.dumps(state["release"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
