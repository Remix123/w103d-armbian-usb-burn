#!/usr/bin/env python3
"""Publish a small, credential-free provenance record for the orchestrated runs."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    source = Path(os.environ["RUNNER_TEMP"]) / "w103d-pipeline-state.json"
    output = Path("outputs/pipeline-result.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        state = json.loads(source.read_text())
    else:
        state = {"status": "pipeline did not write state", "children": []}
    # Deliberately export only provenance and results; environment variables
    # (including the Actions token) are never read or copied.
    output.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
