#!/usr/bin/env python3
"""Validate a workflow run against GitHub's static workflow metadata."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


APPROVED_WORKFLOWS = {
    ".github/workflows/reference-preflight.yml": "Reference image preflight",
    ".github/workflows/compile-kernel.yml": "Compile pinned Ophub W103D kernel",
    ".github/workflows/recover-kernel-artifact.yml": "Recover and verify existing W103D kernel artifact",
    ".github/workflows/build-system.yml": "Rebuild W103D Trixie system with pinned Ophub kernel",
    ".github/workflows/build-usb-burn-image.yml": "Assemble and validate W103D USB Burning Tool image",
    ".github/workflows/deliver-zipped-image.yml": "Deliver validated W103D USB image as ZIP",
}


def validate_run_workflow(
    run: dict,
    repo: str,
    lookup: Callable[[str], dict],
    *,
    require_success: bool = True,
) -> dict:
    """Check immutable path/workflow-ID metadata; run.name is intentionally ignored.

    GitHub's run.name is the display title and can be changed by workflow `run-name`.
    The workflow endpoint's name is the static YAML `name:` value.
    """
    path = run.get("path")
    expected_name = APPROVED_WORKFLOWS.get(path)
    if expected_name is None:
        raise ValueError(f"unapproved workflow path: {path!r}")
    workflow_id = run.get("workflow_id")
    if not isinstance(workflow_id, int) or workflow_id <= 0:
        raise ValueError(f"workflow run has no valid workflow_id: {workflow_id!r}")
    repository = (run.get("repository") or {}).get("full_name")
    if repository != repo:
        raise ValueError(f"workflow run belongs to {repository!r}, expected {repo!r}")
    workflow = lookup(path)
    if workflow.get("id") != workflow_id or workflow.get("path") != path or workflow.get("name") != expected_name:
        raise ValueError(
            f"workflow metadata mismatch for {path}: run workflow_id={workflow_id}, "
            f"API id/path/name={workflow.get('id')!r}/{workflow.get('path')!r}/{workflow.get('name')!r}"
        )
    if require_success and (run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ValueError(f"workflow run has not succeeded: {run.get('status')} {run.get('conclusion')}")
    return workflow


def gh_workflow_lookup(repo: str) -> Callable[[str], dict]:
    cache: dict[str, dict] = {}

    def lookup(path: str) -> dict:
        if path not in cache:
            filename = path.rsplit("/", 1)[-1]
            raw = subprocess.run(
                ["gh", "api", f"repos/{repo}/actions/workflows/{filename}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            cache[path] = json.loads(raw)
        return cache[path]

    return lookup


def validate_run_file(run_path: str, repo: str, *, require_success: bool = True) -> dict:
    run = json.loads(Path(run_path).read_text(encoding="utf-8"))
    workflow = validate_run_workflow(run, repo, gh_workflow_lookup(repo), require_success=require_success)
    print(f"validated workflow id={workflow['id']} path={workflow['path']} static_name={workflow['name']}")
    return run
