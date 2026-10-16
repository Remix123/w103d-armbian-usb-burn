#!/usr/bin/env python3
"""Identify a build by its checked patch configuration, not by 2021 firmware."""
from __future__ import annotations

import re
from kernel_patch import REPO_ROOT, load_patches

CONFIGS = {"v23": "config/kernel-patches.json", "stock": "config/kernel-patches-stock.json"}


def selected_variant(config: str) -> str:
    for variant, path in CONFIGS.items():
        if config == path:
            return variant
    raise ValueError(f"unapproved driver patch config: {config!r}")


def validate_driver_lock(lock: dict, expected_config: str | None = None) -> str:
    config = lock.get("kernel_patch_config")
    variant = selected_variant(config)
    if expected_config is not None and config != expected_config:
        raise ValueError(f"driver config mismatch: expected {expected_config}, found {config}")
    patches = load_patches(REPO_ROOT / config)
    entries = lock.get("kernel_patches")
    if not isinstance(entries, list) or [(p.get("path"), p.get("sha256")) for p in entries] != [(p["path"], p["sha256"]) for p in patches]:
        raise ValueError("source lock has a missing or different driver patch set")
    return variant


def validate_patch_provenance(lock: dict, assembly: dict) -> None:
    validate_driver_lock(lock)
    if assembly.get("kernel_patches") != lock["kernel_patches"]:
        raise ValueError("assembly driver patch provenance differs from the source lock")


def configure_tar_feed(text: str, lock: dict) -> str:
    """The variant feeds publish runtime .tar.gz only, not deb-<version>.tar.gz."""
    variant = validate_driver_lock(lock)
    if f"KERNEL_TAGS='stable_{variant}'" not in text:
        raise ValueError("rebuilt system does not use the selected driver's isolated kernel feed")
    text, count = re.subn(r"^DOWNLOAD_TYPE=.*$", "DOWNLOAD_TYPE='tar'", text, flags=re.M)
    if count != 1:
        raise ValueError("expected exactly one DOWNLOAD_TYPE in Ophub release metadata")
    return text
