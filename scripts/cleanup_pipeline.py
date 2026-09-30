#!/usr/bin/env python3
"""Cancel only child runs correlated to this pipeline; retain source tag as provenance."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


def main() -> None:
    state_path = Path(os.environ["RUNNER_TEMP"]) / "w103d-pipeline-state.json"
    if not state_path.is_file():
        print("pipeline state absent; no child runs to clean")
        return
    state = json.loads(state_path.read_text())
    pipeline_id, tag, head_sha = state.get("pipeline_id", ""), state.get("tag", ""), state.get("head_sha", "")
    if not re.fullmatch(r"pipeline-[0-9]+-[0-9]+", pipeline_id) or tag != f"w103d-{pipeline_id}" or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("refusing cleanup because pipeline identity is malformed")
    repo = os.environ["GITHUB_REPOSITORY"]
    known = {str(child["run_id"]): child["path"] for child in state.get("children", [])}
    # Discover a run whose dispatch was accepted just before cancellation but whose run ID was not yet persisted.
    for workflow in ("reference-preflight.yml", "compile-kernel.yml", "build-system.yml", "build-usb-burn-image.yml", "deliver-zipped-image.yml"):
        path = f".github/workflows/{workflow}"
        endpoint = f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={tag}&per_page=100"
        result = json.loads(gh("api", endpoint))
        for run in result.get("workflow_runs", []):
            if run.get("head_sha") == head_sha and run.get("head_branch") == tag and run.get("path") == path and pipeline_id in run.get("display_title", ""):
                known[str(run["id"])] = path
    active = []
    failures = []
    for run_id, path in known.items():
        run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
        if run.get("path") != path or run.get("head_branch") != tag or run.get("head_sha") != head_sha or pipeline_id not in run.get("display_title", ""):
            print(f"skip run {run_id}: correlation no longer matches")
            continue
        if run.get("status") != "completed":
            active.append(run_id)
            try:
                gh("api", "--method", "POST", f"repos/{repo}/actions/runs/{run_id}/cancel")
            except RuntimeError as error:
                failures.append(f"cancel request failed for child {run_id}: {error}")
                continue
            # Confirm the requested child actually stopped; do not report a
            # failed or still-running cancellation as successful cleanup.
            for _ in range(12):
                current = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
                if current.get("status") == "completed":
                    active.remove(run_id)
                    print(f"confirmed child {run_id} completed with conclusion {current.get('conclusion')}")
                    break
                import time
                time.sleep(5)
            if run_id in active:
                failures.append(f"child {run_id} remains {current.get('status')} after cancellation request")
    if active:
        print(f"retaining source tag {tag}; child runs may still be active: {active}")
    else:
        print(f"source tag {tag} retained as pipeline provenance")
    if failures:
        raise RuntimeError("; ".join(failures))
    print(f"pipeline child cleanup confirmed; originally active children: {len(known)}")


if __name__ == "__main__":
    main()
