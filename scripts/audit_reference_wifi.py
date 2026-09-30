#!/usr/bin/env python3
"""Read-only Wi-Fi/kernel metadata audit of the pinned W103D reference image."""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from inspect_reference import digest, ext4_metadata, sparse_to_raw

SPARSE_MAGIC = b"\x3a\xff\x26\xed"
MODULE_RE = re.compile(r"(?:mt76|mt7663|btmtk|mac80211|cfg80211)", re.I)
FW_NAMES = ("mt7663pr2h_rebb.bin", "mt7663_n9_rebb.bin",
            "mt7663pr2h.bin", "mt7663_n9_v3.bin")
FW_EXPORT_ALLOWLIST = {
    "mt7663_n9_v3.bin": "223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a",
    "mt7663pr2h.bin": "534f2152f9b0f48dfcec9c1727b0bab9a3727a655d76d5ec818cc39a55602336",
}


def run(*argv: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timeout after {timeout}s: {' '.join(argv[:3])}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise RuntimeError(f"exit {result.returncode}: {' '.join(argv[:3])}; {detail}")
    return result


def dbg(raw: Path, expression: str, *, check: bool = True) -> str:
    result = run("debugfs", "-R", expression, str(raw), timeout=60, check=check)
    return result.stdout


def dump(raw: Path, source: str, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    result = run("debugfs", "-R", f"dump {source} {destination}", str(raw),
                 timeout=60, check=False)
    if result.returncode or not destination.is_file():
        destination.unlink(missing_ok=True)
        return False
    return True


def link_target(stat_text: str) -> str | None:
    if not re.search(r"Type:\s*symlink\b", stat_text, re.I):
        return None
    match = re.search(r"(?:Fast )?link dest:\s*[\"']?([^\"'\r\n]+)", stat_text, re.I)
    return match.group(1).strip() if match else ""


def resolve_link_path(current: str, target: str, allowed_roots: tuple[str, ...]) -> str | None:
    resolved = posixpath.normpath(target if target.startswith("/")
                                  else posixpath.join(posixpath.dirname(current), target))
    if any(resolved == root or resolved.startswith(root.rstrip("/") + "/")
           for root in allowed_roots):
        return resolved
    return None


def extract_firmware(raw: Path, requested_path: str, allowed_roots: tuple[str, ...],
                     destination: Path) -> dict:
    current = posixpath.normpath(requested_path)
    chain = [current]
    for _ in range(8):
        stat_result = run("debugfs", "-R", f"stat {current}", str(raw), timeout=30, check=False)
        stat_text = stat_result.stdout + "\n" + stat_result.stderr
        type_match = re.search(r"Type:\s*(\w+)", stat_text, re.I)
        if not type_match or "not found" in stat_text.lower() or "no such file" in stat_text.lower():
            return {"requested_path": requested_path, "resolved_path": None,
                    "path_chain": chain, "present": False,
                    "error": "debugfs stat could not resolve requested path"}
        if type_match.group(1).lower() != "symlink":
            if type_match.group(1).lower() != "regular":
                return {"requested_path": requested_path, "resolved_path": None,
                        "path_chain": chain, "present": False,
                        "error": f"resolved inode type is {type_match.group(1)}, not regular"}
            if not dump(raw, current, destination):
                return {"requested_path": requested_path, "resolved_path": current,
                        "path_chain": chain, "present": False,
                        "error": "debugfs failed to dump resolved firmware target"}
            return {"requested_path": requested_path, "resolved_path": current,
                    "path_chain": chain, "present": True,
                    "size": destination.stat().st_size, "sha256": digest(destination)}
        target = link_target(stat_text)
        if not target:
            return {"requested_path": requested_path, "resolved_path": None,
                    "path_chain": chain, "present": False,
                    "error": "symlink target missing or unreadable"}
        next_path = resolve_link_path(current, target, allowed_roots)
        if not next_path:
            return {"requested_path": requested_path, "resolved_path": None,
                    "path_chain": chain, "present": False,
                    "error": "symlink target escapes allowed firmware directory"}
        current = next_path
        if current in chain:
            return {"requested_path": requested_path, "resolved_path": None,
                    "path_chain": chain + [current], "present": False,
                    "error": "firmware symlink loop"}
        chain.append(current)
    return {"requested_path": requested_path, "resolved_path": None,
            "path_chain": chain, "present": False,
            "error": "firmware symlink depth exceeds 8"}


def export_allowed_firmware(raw: Path, candidates: list[dict], destination: Path,
                            reference_lock: dict, source_run: str,
                            source_run_url: str, scratch: Path) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("firmware export directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    by_name = {Path(item.get("requested_path", "")).name: item for item in candidates
               if item.get("requested_path")}
    allowed_roots = ("/usr/lib/firmware/mediatek", "/lib/firmware/mediatek")
    exported: list[Path] = []
    manifest = {"reference_release_tag": reference_lock["release_tag"],
                "reference_asset_name": reference_lock["asset_name"],
                "reference_image_sha256": reference_lock["image_sha256"],
                "source_workflow_run_id": str(source_run),
                "source_workflow_run_url": source_run_url,
                "files": []}
    try:
        for filename, expected_hash in FW_EXPORT_ALLOWLIST.items():
            candidate = by_name.get(filename)
            requested = candidate.get("requested_path") if candidate else None
            if not candidate or not candidate.get("present") or not requested:
                raise RuntimeError(f"allowlisted firmware is unavailable: {filename}")
            staged = scratch / f"export-{filename}"
            resolved = extract_firmware(raw, requested, allowed_roots, staged)
            if not resolved.get("present"):
                raise RuntimeError(f"cannot extract {filename}: {resolved.get('error')}")
            if resolved["sha256"] != expected_hash:
                raise RuntimeError(f"SHA-256 mismatch for {filename}: {resolved['sha256']}")
            target = destination / filename
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"refusing to overwrite export target {filename}")
            shutil.copyfile(staged, target)
            exported.append(target)
            output_hash = digest(target)
            if output_hash != expected_hash:
                raise RuntimeError(f"post-copy SHA-256 mismatch for {filename}")
            manifest["files"].append({"name": filename, "size": target.stat().st_size,
                "sha256": output_hash, "expected_sha256": expected_hash,
                "source_requested_path": requested,
                "source_resolved_path": resolved["resolved_path"]})
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        exported.append(manifest_path)
        return manifest
    except Exception:
        for path in exported:
            path.unlink(missing_ok=True)
        raise


def expected_default_selection(candidates: dict[str, dict]) -> dict:
    primary_rom = "mt7663pr2h.bin"
    fallback_rom = "mt7663pr2h_rebb.bin"
    primary_n9 = "mt7663_n9_v3.bin"
    fallback_n9 = "mt7663_n9_rebb.bin"
    primary_present = bool(candidates.get(primary_rom, {}).get("present"))
    selected_rom = primary_rom if primary_present else fallback_rom
    selected_n9 = primary_n9 if primary_present else fallback_n9
    return {
        "selector_default_prefer_offload_fw": True,
        "runtime_prefer_offload_fw_override": "unknown",
        "expected_by_reference_availability": "offload" if primary_present else "fallback",
        "assumption": "the expected primary ROM patch loads successfully; file presence alone does not prove MCU accepted it",
        "actual_loaded_rom_and_n9": "unknown without runtime firmware-loading logs or controller evidence",
        "rom_requested_by_default": primary_rom,
        "fallback_rom": fallback_rom,
        "n9_paired_with_rom": selected_n9,
        "required_for_selected_default_path": [selected_rom, selected_n9],
        "optional_unselected_alternatives": [fallback_rom, fallback_n9] if primary_present else [],
    }


def debugfs_names(raw: Path, path: str) -> list[str]:
    result = run("debugfs", "-R", f"ls -l {path}", str(raw), timeout=60, check=False)
    if result.returncode:
        return []
    names = []
    for line in result.stdout.splitlines():
        match = re.search(r"\s([^\s]+)\s*$", line)
        if match and match.group(1) not in (".", "..") and not line.startswith("debugfs "):
            names.append(match.group(1))
    return names


def raw_partition(src: Path, dst: Path) -> dict:
    with src.open("rb") as stream:
        magic = stream.read(4)
    if magic == SPARSE_MAGIC:
        return {"format": "android_sparse", **sparse_to_raw(src, dst)}
    # Components that are already raw are consumed in place, never copied or modified.
    return {"format": "raw", "raw_bytes": src.stat().st_size,
            "sha256": digest(src), "path_used": str(src)}


def list_modules(root: Path, release: str, scratch: Path, report: dict) -> None:
    dep_path = f"{root}/{release}/modules.dep"
    dep_local = scratch / f"modules-dep-{re.sub(r'[^A-Za-z0-9_.-]', '_', release)}"
    if not dump(report["_root_raw"], dep_path, dep_local):
        report["errors"].append(f"cannot read {dep_path}")
        return
    text = dep_local.read_text(errors="replace")
    module_paths = sorted({line.partition(":")[0] for line in text.splitlines()
                           if ".ko" in line.partition(":")[0]})
    dependencies: dict[str, list[str]] = {}
    for line in text.splitlines():
        module, sep, rest = line.partition(":")
        if sep:
            dependencies[module] = [item for item in rest.split()
                if item.endswith((".ko", ".ko.xz", ".ko.zst", ".ko.gz"))]
    report["modules"]["inventory"].extend(f"{root}/{release}/{item}" for item in module_paths)

    selected = {path for path in module_paths if MODULE_RE.search(path)}
    # Include the transitive dependency closure only for selected wireless/BT modules.
    pending = list(selected)
    while pending:
        current = pending.pop()
        for dependency in dependencies.get(current, []):
            if dependency in dependencies and dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)

    for rel_path in sorted(selected):
        path = f"{root}/{release}/{rel_path}"
        local = scratch / "modules" / re.sub(r"[^A-Za-z0-9_.-]+", "_", path)
        present = dump(report["_root_raw"], path, local)
        record = {"path": path, "present": present, "dependencies": dependencies.get(rel_path, [])}
        if present:
            record.update({"size": local.stat().st_size, "sha256": digest(local)})
            if not path.endswith((".ko.xz", ".ko.zst", ".ko.gz")):
                record["uncompressed_content_sha256"] = digest(local)
            else:
                record["uncompressed_content_sha256"] = None
                record["uncompressed_content_note"] = "compressed module retained as-is; no decompression performed"
            for field in ("version", "vermagic", "firmware"):
                result = run("modinfo", "-F", field, str(local), timeout=30, check=False)
                value = result.stdout.strip()
                if field == "firmware":
                    record["firmware_metadata"] = [line.strip() for line in value.splitlines() if line.strip()] if result.returncode == 0 else None
                else:
                    record[f"{field}_metadata"] = value if result.returncode == 0 else None
                if result.returncode:
                    detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:300]
                    message = f"modinfo {field} failed for {path} ({result.returncode}): {detail}"
                    record.setdefault("metadata_errors", []).append(message)
                    report["errors"].append(message)
        else:
            report["errors"].append(f"cannot dump selected module {path}")
        report["modules"]["selected_hashes"].append(record)


def audit(components: Path, report: dict, scratch: Path,
          export_firmware_dir: Path | None = None,
          reference_lock: dict | None = None,
          source_run: str | None = None,
          source_run_url: str | None = None) -> None:
    data = components / "data.PARTITION"
    system = components / "system.PARTITION"
    if not data.is_file() or not system.is_file():
        report["errors"].append("AMpack data.PARTITION/system.PARTITION missing")
        return

    root_raw, boot_raw = scratch / "rootfs.raw", scratch / "bootfs.raw"
    report["rootfs"]["component_sha256"] = digest(data)
    report["rootfs"]["partition"] = raw_partition(data, root_raw)
    root_is_sparse = report["rootfs"]["partition"]["format"] == "android_sparse"
    root_image = root_raw if root_is_sparse else data
    report["rootfs"]["ext4"] = ext4_metadata(root_image)
    if not report["rootfs"]["ext4"]:
        report["errors"].append("data.PARTITION is not a recognized ext4 filesystem")
        return
    report["rootfs"]["superblock"] = report["rootfs"].pop("ext4")
    report["_root_raw"] = root_image
    report["rootfs"]["root_listing"] = dbg(root_image, "ls -l /").splitlines()[:70]

    report["bootfs"]["component_sha256"] = digest(system)
    report["bootfs"]["partition"] = raw_partition(system, boot_raw)
    boot_image = boot_raw if report["bootfs"]["partition"]["format"] == "android_sparse" else system
    with tempfile.TemporaryDirectory(prefix="w103d-wifi-boot-") as boot_tmp:
        boot_dir = Path(boot_tmp)
        run("mcopy", "-s", "-i", str(boot_image), "::*", str(boot_dir) + "/", timeout=180)
        boot_files = [path for path in boot_dir.rglob("*") if path.is_file()]
        report["bootfs"]["file_count"] = len(boot_files)
        config_files = sorted(path for path in boot_files if re.fullmatch(r"config-.+", path.name))
        report["reference_kernel"]["config_files"] = []
        for config_file in config_files:
            version = config_file.name.removeprefix("config-")
            cfg_hash = digest(config_file)
            report["reference_kernel"]["config_files"].append(
                {"path": config_file.relative_to(boot_dir).as_posix(),
                 "version": version, "sha256": cfg_hash})
            if report["reference_kernel"]["version"] is None:
                report["reference_kernel"]["version"] = version
                config = config_file.read_text(errors="replace")
                report["kernel_config"] = [line for line in config.splitlines()
                    if re.match(r"#?\s*CONFIG_(?:MT76|MT7663|MMC|PSI|WIFI|CFG80211|MAC80211)", line)]
        if not config_files:
            report["errors"].append("boot FAT contains no config-* kernel configuration")

        script_files = sorted(path for path in boot_files
                              if path.name in ("uEnv.txt", "emmc_autoscript.cmd", "boot.cmd"))
        for path in script_files:
            content = path.read_text(errors="replace")
            lines = [line.strip() for line in content.splitlines()
                     if re.search(r"zImage|initrd|ramdisk|dtb", line, re.I)]
            report["reference_kernel"]["boot_script_evidence"].append(
                {"path": path.relative_to(boot_dir).as_posix(),
                 "sha256": digest(path), "references": lines[:80]})
        zimages = [path for path in boot_files if path.name == "zImage"]
        for path in zimages[:3]:
            strings = re.findall(rb"Linux version [^\x00\r\n]{1,180}", path.read_bytes())
            report["reference_kernel"]["zimage_version_strings"].extend(
                item.decode("ascii", "replace") for item in strings[:5])

        dtbs = [path for path in boot_files if path.name == "meson-g12a-w103d.dtb"]
        if dtbs:
            target = dtbs[0]
            report["dtb"] = {"path": target.relative_to(boot_dir).as_posix(),
                             "size": target.stat().st_size, "sha256": digest(target)}
            try:
                dts = run("dtc", "-I", "dtb", "-O", "dts", str(target), timeout=30).stdout
                properties = []
                stack: list[str] = []
                wanted = re.compile(r"max-frequency|cap-sd-highspeed|sd-uhs-|bus-width|"
                                    r"pwrseq|reset|zte,sdio-fixed-1-8v|sdio-transfer", re.I)
                for line in dts.splitlines():
                    stripped = line.strip()
                    if stripped.endswith("{"):
                        stack.append(stripped[:-1].strip())
                    elif wanted.search(stripped):
                        properties.append({"node": "/" + "/".join(stack), "property": stripped[:500]})
                    if stripped.startswith("}") and stack:
                        stack.pop()
                report["dtb"]["sdio_properties"] = properties[:100]
            except RuntimeError as exc:
                report["errors"].append(f"cannot decode target DTB: {exc}")
        else:
            report["errors"].append("boot-referenced meson-g12a-w103d.dtb missing")

    module_dirs = [name for name in debugfs_names(root_image, "/usr/lib/modules")]
    modules_root = "/usr/lib/modules" if module_dirs else "/lib/modules"
    if not module_dirs:
        module_dirs = debugfs_names(root_image, modules_root)
    report["modules"]["root_path"] = modules_root
    report["modules"]["kernel_release_directories"] = module_dirs
    if not module_dirs:
        report["errors"].append("no module directories found under /usr/lib/modules or /lib/modules")
    for release in module_dirs:
        list_modules(modules_root, release, scratch, report)
    report["_root_raw"] = root_image

    fw_path = "/usr/lib/firmware/mediatek"
    fw_entries = debugfs_names(root_image, fw_path)
    if not fw_entries:
        fw_path = "/lib/firmware/mediatek"
        fw_entries = debugfs_names(root_image, fw_path)
    report["firmware"]["root_path"] = fw_path
    allowed_mtk_roots = ("/usr/lib/firmware/mediatek", "/lib/firmware/mediatek")
    candidate_errors: dict[str, str] = {}
    for name in FW_NAMES:
        possible = [f"{fw_path}/{name}"]
        possible.extend(f"{fw_path}/{entry}/{name}" for entry in fw_entries)
        found = None
        for path in possible:
            local = scratch / "firmware" / f"candidate-{name}"
            result = extract_firmware(root_image, path, allowed_mtk_roots, local)
            if result["present"]:
                found = result
                break
            if result.get("error") != "debugfs stat could not resolve requested path":
                candidate_errors[name] = result["error"]
        report["firmware"]["candidates"].append(found or {"name": name, "present": False})

    # Resolve only firmware named by the target mt7663s module's modinfo metadata.
    target_modules = [item for item in report["modules"]["selected_hashes"]
                      if "mt7663s" in item["path"].lower() and item.get("present")]
    target_firmware = sorted({name for item in target_modules
                              for name in (item.get("firmware_metadata") or [])})
    report["firmware"]["module_declared"] = []
    report["firmware"]["limitations"] = []
    candidate_by_name = {item.get("name") or Path(item.get("requested_path", "")).name: item
                         for item in report["firmware"]["candidates"]}
    if not target_modules or not target_firmware:
        limitation = "mt7663s module firmware declarations unavailable; actual loaded firmware cannot be identified"
        report["firmware"]["limitations"].append(limitation)
        report["errors"].append(limitation)
    else:
        for index, declared in enumerate(target_firmware):
            name = Path(declared).name
            record = dict(candidate_by_name.get(name, {"name": name, "present": False}))
            record["declared_name"] = declared
            record["required_by_default_selector"] = False
            record["availability_status"] = "available" if record.get("present") else "absent"
            report["firmware"]["module_declared"].append(record)

        report["firmware"]["selection"] = expected_default_selection(candidate_by_name)
        selection = report["firmware"]["selection"]
        primary_present = candidate_by_name.get("mt7663pr2h.bin", {}).get("present", False)
        selected_rom, selected_n9 = selection["required_for_selected_default_path"]
        if not primary_present:
            detail = candidate_errors.get("mt7663pr2h.bin", "not present")
            report["errors"].append(f"default primary Wi-Fi ROM patch missing: mt7663pr2h.bin ({detail})")
        for required in (selected_rom, selected_n9):
            if not candidate_by_name.get(required, {}).get("present"):
                detail = candidate_errors.get(required, "not present")
                report["errors"].append(f"required selected Wi-Fi firmware missing: {required} ({detail})")
        for item in report["firmware"]["module_declared"]:
            item["required_by_default_selector"] = Path(item["declared_name"]).name in (selected_rom, selected_n9)

        known_candidate_names = set(candidate_by_name)
        fw_base = "/usr/lib/firmware" if debugfs_names(root_image, "/usr/lib/firmware") else "/lib/firmware"
        allowed_fw_roots = ("/usr/lib/firmware", "/lib/firmware")
        for index, declared in enumerate(target_firmware):
            name = Path(declared).name
            if name in known_candidate_names:
                continue
            requested = posixpath.normpath(posixpath.join(fw_base, declared.lstrip("/")))
            record = extract_firmware(root_image, requested, allowed_fw_roots,
                                      scratch / "firmware" / f"declared-extra-{index}")
            record["declared_name"] = declared
            record["availability_status"] = "available" if record.get("present") else "unknown"
            report["firmware"]["module_declared"].append(record)
            if not record.get("present"):
                limitation = f"additional module-declared firmware not resolved: {declared}"
                report["firmware"]["limitations"].append(limitation)

        if export_firmware_dir:
            try:
                manifest = export_allowed_firmware(
                    root_image, report["firmware"]["candidates"], export_firmware_dir,
                    reference_lock or {}, source_run or "", source_run_url or "", scratch)
                report["firmware"]["export"] = {"success": True, "manifest": manifest}
            except Exception as exc:
                report["firmware"]["export"] = {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:700]}"}
                report["errors"].append(f"allowlisted firmware export failed: {report['firmware']['export']['error']}")

    status_path = "/usr/lib/dpkg/status"
    status_local = scratch / "dpkg-status"
    if not dump(root_image, status_path, status_local):
        status_path = "/var/lib/dpkg/status"
        if not dump(root_image, status_path, status_local):
            report["errors"].append("package status database missing")
    if status_local.is_file():
        wanted = {"network-manager", "wpasupplicant", "wpa-supplicant"}
        for stanza in status_local.read_text(errors="replace").split("\n\n"):
            fields = dict(re.findall(r"^(Package|Version|Status):\s*(.+)$", stanza, re.M))
            if fields.get("Package", "").lower() in wanted and fields.get("Status", "").endswith("installed"):
                report["packages"].append(fields)

    if not any("mt7663s" in item["path"].lower() and item.get("present")
               for item in report["modules"]["selected_hashes"]):
        report["errors"].append("expected mt7663s module absent from selected module hashes")
    if not report["modules"]["inventory"]:
        report["errors"].append("kernel module inventory is empty")
    if not report["kernel_config"]:
        report["errors"].append("no relevant MT76/MMC/PSI kernel config entries found")
    installed_packages = {item.get("Package", "").lower() for item in report["packages"]}
    if "network-manager" not in installed_packages:
        report["errors"].append("NetworkManager package version missing from dpkg status")
    if not installed_packages.intersection({"wpasupplicant", "wpa-supplicant"}):
        report["errors"].append("wpa_supplicant package version missing from dpkg status")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--export-firmware-dir", type=Path)
    parser.add_argument("--reference-lock", type=Path)
    parser.add_argument("--source-run")
    parser.add_argument("--source-run-url")
    args = parser.parse_args()
    export_requested = args.export_firmware_dir is not None
    if export_requested and not all((args.reference_lock, args.source_run, args.source_run_url)):
        parser.error("firmware export requires --reference-lock, --source-run, and --source-run-url")
    reference_lock = json.loads(args.reference_lock.read_text()) if args.reference_lock else None
    report = {
        "audit_complete": False,
        "scope": "read-only metadata and hashes; no image modifications, mounts, or binary execution",
        "reference_kernel": {"version": None, "config_files": [], "boot_script_evidence": [],
                             "zimage_version_strings": []},
        "rootfs": {}, "bootfs": {}, "modules": {"root_path": None,
            "kernel_release_directories": [], "inventory": [], "selected_hashes": []},
        "firmware": {"root_path": None, "candidates": []},
        "kernel_config": [], "packages": [], "dtb": {}, "errors": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="w103d-wifi-audit-") as td:
            audit(args.components.resolve(strict=True), report, Path(td),
                  args.export_firmware_dir, reference_lock, args.source_run,
                  args.source_run_url)
    except Exception as exc:  # Always leave a machine-readable incomplete report for the artifact.
        report["errors"].append(f"audit aborted: {type(exc).__name__}: {str(exc)[:700]}")
    report["audit_complete"] = not report["errors"]
    report.pop("_root_raw", None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"audit_complete": report["audit_complete"],
                      "kernel_version": report["reference_kernel"]["version"],
                      "module_inventory_count": len(report["modules"]["inventory"]),
                      "selected_module_hash_count": len(report["modules"]["selected_hashes"]),
                      "firmware_present": sum(x.get("present", False) for x in report["firmware"]["candidates"]),
                      "errors": report["errors"], "report": str(args.out)}, indent=2))
    if export_requested and not report.get("firmware", {}).get("export", {}).get("success"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
