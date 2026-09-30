#!/usr/bin/env python3
"""Run the validated W103D Actions stages in order with strict run correlation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


WORKFLOWS = {
    "preflight": ("reference-preflight.yml", ".github/workflows/reference-preflight.yml", "Reference image preflight"),
    "compile": ("compile-kernel.yml", ".github/workflows/compile-kernel.yml", "Compile pinned Ophub W103D kernel"),
    "system": ("build-system.yml", ".github/workflows/build-system.yml", "Rebuild W103D Trixie system with pinned Ophub kernel"),
    "usb": ("build-usb-burn-image.yml", ".github/workflows/build-usb-burn-image.yml", "Assemble and validate W103D USB Burning Tool image"),
    "zip": ("deliver-zipped-image.yml", ".github/workflows/deliver-zipped-image.yml", "Deliver validated W103D USB image as ZIP"),
}
GLOBAL_DEADLINE = 0.0


def numeric_run_id(value: str | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{label} must contain only decimal digits")
    return value


def select_correlated_run(runs: list[dict], *, pipeline_id: str, tag: str, head_sha: str, workflow_path: str) -> dict:
    matches = [run for run in runs if
        run.get("event") == "workflow_dispatch" and
        run.get("head_branch") == tag and
        run.get("head_sha") == head_sha and
        run.get("path") == workflow_path and
        pipeline_id in run.get("display_title", "")]
    if len(matches) != 1:
        raise ValueError(f"expected one uniquely correlated {workflow_path} run; found {[(r.get('id'),r.get('display_title')) for r in matches]}")
    return matches[0]


def checked_run(run_id: str, path: str, name: str) -> dict:
    run = json.loads(gh("api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}"))
    if str(run.get("id")) != run_id or run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError(f"reuse run {run_id} is not completed successfully")
    if run.get("path") != path or run.get("name") != name:
        raise ValueError(f"reuse run {run_id} is not from the approved workflow {path}: {run.get('path')} {run.get('name')}")
    return run


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def save_state(state: dict) -> None:
    Path(os.environ["RUNNER_TEMP"], "w103d-pipeline-state.json").write_text(json.dumps(state, indent=2) + "\n")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(text + "\n")


def api_runs(workflow: str, tag: str) -> list[dict]:
    repo = os.environ["GITHUB_REPOSITORY"]
    endpoint = f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={quote(tag, safe='')}&per_page=100"
    return json.loads(gh("api", endpoint)).get("workflow_runs", [])


def wait_for_child(stage: str, input_args: list[str], state: dict, stage_timeout: int) -> str:
    workflow, path, title = WORKFLOWS[stage]
    pipeline_id, tag, head_sha = state["pipeline_id"], state["tag"], state["head_sha"]
    command = ["workflow", "run", workflow, "--repo", os.environ["GITHUB_REPOSITORY"], "--ref", tag]
    for pair in [*input_args, ["pipeline_id", pipeline_id]]:
        command.extend(["--field", f"{pair[0]}={pair[1]}"])
    state["pending_child"] = {"stage": stage, "workflow": workflow, "path": path, "tag": tag,
                              "head_sha": head_sha, "pipeline_id": pipeline_id, "inputs": input_args}
    save_state(state)
    gh(*command)
    deadline = min(time.monotonic() + stage_timeout, GLOBAL_DEADLINE)
    run = None
    while time.monotonic() < deadline:
        try:
            run = select_correlated_run(api_runs(workflow, tag), pipeline_id=pipeline_id, tag=tag, head_sha=head_sha, workflow_path=path)
            break
        except ValueError as error:
            if "found []" not in str(error):
                raise
            time.sleep(10)
    if run is None:
        raise TimeoutError(f"timed out waiting for uniquely correlated child run: {stage}")
    child = {"stage": stage, "run_id": str(run["id"]), "path": path, "title": title, "url": run.get("html_url"), "head_sha": head_sha, "tag": tag}
    state["children"].append(child)
    state["active_child_run_id"] = child["run_id"]
    state["pending_child"] = None
    save_state(state)
    summary(f"- {stage}: dispatched run [{child['run_id']}]({child['url']}) on `{tag}` at `{head_sha}`")
    deadline = min(time.monotonic() + stage_timeout, GLOBAL_DEADLINE)
    last_status = None
    while time.monotonic() < deadline:
        fresh = json.loads(gh("api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{child['run_id']}"))
        if fresh.get("path") != path or fresh.get("head_branch") != tag or fresh.get("head_sha") != head_sha or pipeline_id not in fresh.get("display_title", ""):
            raise ValueError(f"child run provenance changed after dispatch: {fresh}")
        status = fresh.get("status")
        if status != last_status:
            summary(f"  - status: `{status}`" + (f" / `{fresh.get('conclusion')}`" if status == "completed" else ""))
            last_status = status
        if status == "completed":
            state["active_child_run_id"] = None
            save_state(state)
            if fresh.get("conclusion") != "success":
                raise RuntimeError(f"child workflow {stage} run {child['run_id']} concluded {fresh.get('conclusion')}: {child['url']}")
            return child["run_id"]
        time.sleep(20)
    raise TimeoutError(f"child workflow {stage} run {child['run_id']} exceeded {stage_timeout}s: {child['url']}")


def main() -> None:
    global GLOBAL_DEADLINE
    GLOBAL_DEADLINE = time.monotonic() + 350 * 60
    repo = os.environ["GITHUB_REPOSITORY"]
    head_sha = os.environ["GITHUB_SHA"]
    pipeline_id = f"pipeline-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    tag = f"w103d-{pipeline_id}"
    reuse_system = numeric_run_id(os.environ.get("REUSE_SYSTEM_RUN_ID"), "reuse_system_run_id")
    reuse_usb = numeric_run_id(os.environ.get("REUSE_USB_RUN_ID"), "reuse_usb_run_id")
    if reuse_system and reuse_usb:
        raise ValueError("provide at most one reuse run ID")
    state = {"pipeline_id": pipeline_id, "tag": tag, "head_sha": head_sha, "children": [], "active_child_run_id": None}
    save_state(state)
    subprocess.run(["git", "tag", tag, head_sha], check=True)
    subprocess.run(["git", "push", "origin", f"refs/tags/{tag}"], check=True)
    state["tag_created"] = True
    save_state(state)
    summary(f"## W103D source build pipeline `{pipeline_id}`\n\nFixed source ref: `{tag}` -> `{head_sha}`. Each stage is a separate workflow run and is correlated by this tag, SHA, workflow path, and token. Any failed stage stops delivery.")

    if reuse_usb:
        run = checked_run(reuse_usb, WORKFLOWS["usb"][1], WORKFLOWS["usb"][2])
        summary(f"- reusing verified USB run [{reuse_usb}]({run['html_url']})")
        usb_run = reuse_usb
    else:
        if reuse_system:
            run = checked_run(reuse_system, WORKFLOWS["system"][1], WORKFLOWS["system"][2])
            summary(f"- reusing verified system run [{reuse_system}]({run['html_url']})")
            system_run = reuse_system
        else:
            wait_for_child("preflight", [], state, 30 * 60)
            kernel_run = wait_for_child("compile", [["run_kernel_build", "true"]], state, 330 * 60)
            system_run = wait_for_child("system", [["kernel_run_id", kernel_run]], state, 170 * 60)
        usb_run = wait_for_child("usb", [["system_run_id", system_run]], state, 170 * 60)
    zip_run = wait_for_child("zip", [["usb_run_id", usb_run]], state, 170 * 60)
    zip_run_info = checked_run(zip_run, WORKFLOWS["zip"][1], WORKFLOWS["zip"][2])
    artifacts = json.loads(gh("api", f"repos/{repo}/actions/runs/{zip_run}/artifacts?per_page=100"))["artifacts"]
    zip_artifacts = [a for a in artifacts if a.get("name", "").endswith("_USB_Burning_Tool.zip") and not a.get("expired")]
    if len(zip_artifacts) != 1:
        raise ValueError(f"expected exactly one downloadable final ZIP artifact for run {zip_run}: {artifacts}")
    zipped = zip_artifacts[0]
    zip_metadata = json.loads(gh("api", f"repos/{repo}/actions/artifacts/{zipped['id']}"))
    sidecars = [a for a in artifacts if a.get("name") == f"{zip_metadata.get('name')}.sha256" and not a.get("expired")]
    if len(sidecars) != 1:
        raise ValueError(f"expected exactly one downloadable external SHA-256 sidecar: {artifacts}")
    sidecar = sidecars[0]
    report_dir = Path(os.environ["RUNNER_TEMP"]) / "zip-result"
    gh("run", "download", zip_run, "--repo", repo, "--name", f"w103d-zipped-delivery-verification-{zip_run}", "--dir", str(report_dir))
    validation = report_dir / "zip-delivery-validation.txt"
    if not validation.is_file():
        raise ValueError("ZIP run did not publish its final readback validation report")
    fields = dict(line.split("=", 1) for line in validation.read_text().splitlines() if "=" in line)
    if int(fields.get("zip_artifact_id", "0")) != int(zipped["id"]):
        raise ValueError("ZIP verification report artifact ID differs from the selected final ZIP artifact")
    if fields.get("source_usb_run_id") != usb_run:
        raise ValueError("ZIP verification report cites a different USB source run")
    zip_url = f"https://github.com/{repo}/actions/runs/{zip_run}/artifacts/{zipped['id']}"
    sidecar_url = f"https://github.com/{repo}/actions/runs/{zip_run}/artifacts/{sidecar['id']}"
    image_name, zip_name = fields.get("image_basename", ""), fields.get("zip_basename", "")
    image_sha, zip_sha = fields.get("image_sha256", ""), fields.get("zip_sha256", "")
    if not all((image_name, zip_name, re.fullmatch(r"[0-9a-f]{64}", image_sha), re.fullmatch(r"[0-9a-f]{64}", zip_sha))):
        raise ValueError(f"ZIP readback validation report is incomplete: {fields}")
    if (zip_metadata.get("name") != zip_name or zip_name != image_name.removesuffix(".img") + ".zip"
            or str(zip_metadata.get("size_in_bytes")) != fields.get("zip_bytes")
            or zip_metadata.get("digest") != f"sha256:{zip_sha}"):
        raise ValueError(f"ZIP metadata, versioned basename, size or digest differs from readback report: {zip_metadata} {fields}")
    state["result"] = {"system_run_id": locals().get("system_run"), "usb_run_id": usb_run, "zip_run_id": zip_run,
                       "zip_artifact_id": str(zipped["id"]), "zip_url": zip_url,
                       "zip_sidecar_artifact_id": str(sidecar["id"]), "zip_sidecar_url": sidecar_url,
                       "zip_name": zip_name, "zip_sha256": zip_sha, "image_name": image_name, "image_sha256": image_sha}
    save_state(state)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"pipeline_id={pipeline_id}\nusb_run_id={usb_run}\nzip_run_id={zip_run}\nzip_artifact_id={zipped['id']}\nzip_url={zip_url}\nzip_sidecar_url={sidecar_url}\nzip_name={zip_name}\nzip_sha256={zip_sha}\nimage_name={image_name}\nimage_sha256={image_sha}\n")
    summary(f"\n## Final verified downloads\n\n- ZIP: [{zip_name}]({zip_url})\n- ZIP SHA-256 sidecar: [download]({sidecar_url})\n- ZIP SHA-256: `{zip_sha}`\n- IMG inside ZIP: `{image_name}`\n- IMG SHA-256: `{image_sha}`\n- Source USB run: [#{usb_run}](https://github.com/{repo}/actions/runs/{usb_run})\n- ZIP run: [{zip_run}]({zip_run_info['html_url']})")


if __name__ == "__main__":
    main()
