#!/usr/bin/env python3
"""Export only the locked reference image's MT7663 Wi-Fi module dependency set."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from audit_reference_wifi import debugfs_names, digest, dump, raw_partition


EXPECTED_RELEASE = "6.18.52-ophub"
MODULES = {
    "cfg80211": "kernel/net/wireless/cfg80211.ko.xz",
    "mac80211": "kernel/net/mac80211/mac80211.ko.xz",
    "mt76": "kernel/drivers/net/wireless/mediatek/mt76/mt76.ko.xz",
    "mt76_connac_lib": "kernel/drivers/net/wireless/mediatek/mt76/mt76-connac-lib.ko.xz",
    "mt76_sdio": "kernel/drivers/net/wireless/mediatek/mt76/mt76-sdio.ko.xz",
    "mt7615_common": "kernel/drivers/net/wireless/mediatek/mt76/mt7615/mt7615-common.ko.xz",
    "mt7663_usb_sdio_common": "kernel/drivers/net/wireless/mediatek/mt76/mt7615/mt7663-usb-sdio-common.ko.xz",
    "mt7663s": "kernel/drivers/net/wireless/mediatek/mt76/mt7663s/mt7663s.ko.xz",
}
W103D_MARKERS = (
    "mt7663s_w103d_mcu_send_msg",
    "mt7663s_w103d_cmd_no_ack",
    "mt7663s_w103d_should_wait_resp",
    "mt7663s_w103d_sdio_write",
    "mt7663s_w103d_sync_bss_rlm",
    "mt7663s_w103d_tx_prepare_skb",
    "mt7663s_w103d_fix_5g_tx_rate",
    "mt7663s_w103d_add_dev_info",
    "DEV_INFO_UPDATE",
    "BSS_INFO_UPDATE",
)
MODPROBE_DIRS = ("/etc/modprobe.d", "/usr/lib/modprobe.d", "/lib/modprobe.d")


def command(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result


def sha256(path: Path) -> str:
    return digest(path)


def dump_module(raw: Path, root: str, release: str, relative: str,
                destination: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.+/-]+\.ko\.xz", relative):
        raise ValueError(f"module path outside allowlist grammar: {relative}")
    source = f"{root}/{release}/{relative}"
    if not dump(raw, source, destination):
        raise RuntimeError(f"cannot extract allowlisted module: {source}")


def decompress_xz(source: Path, target: Path) -> None:
    with source.open("rb") as src, target.open("xb") as dst:
        result = subprocess.run(("xz", "-dc"), stdin=src, stdout=dst,
                                stderr=subprocess.PIPE, check=False)
    if result.returncode:
        target.unlink(missing_ok=True)
        detail = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"xz decompression failed for {source.name}: {detail}")


def modinfo_fields(path: Path) -> dict:
    result = {}
    for field in ("name", "version", "vermagic", "firmware", "parm"):
        proc = command("modinfo", "-F", field, str(path), check=False)
        result[field] = {
            "available": proc.returncode == 0,
            "values": [line for line in proc.stdout.splitlines() if line.strip()],
        }
    result["parm_value_scope"] = (
        "modinfo parm lists declarations/descriptions only; it does not establish "
        "compiled defaults or runtime parameter values"
    )
    return result


def parse_comment_strings(output: str) -> list[str]:
    comments = []
    for line in output.splitlines():
        match = re.search(r"\[\s*[0-9a-fA-F]+\s*\]\s*(.*)$", line)
        if match and match.group(1).strip():
            comments.append(match.group(1).strip())
    return comments


def elf_metadata(path: Path) -> dict:
    notes = command("aarch64-linux-gnu-readelf", "-n", str(path), check=False)
    comment = command("aarch64-linux-gnu-readelf", "-p", ".comment", str(path), check=False)
    header = command("aarch64-linux-gnu-readelf", "-h", str(path), check=False)
    build = re.search(r"Build ID:\s*([0-9a-fA-F]+)", notes.stdout)
    comments = parse_comment_strings(comment.stdout)
    machine = re.search(r"^\s*Machine:\s*(.+)$", header.stdout, re.M)
    elf_class = re.search(r"^\s*Class:\s*(.+)$", header.stdout, re.M)
    return {
        "gnu_build_id": build.group(1).lower() if build else None,
        "comment_section_strings": comments,
        "comment_section_raw": comment.stdout.strip(),
        "compiler_info_scope": ".comment strings when retained; absence does not identify compiler",
        "elf_machine": machine.group(1).strip() if machine else None,
        "elf_class": elf_class.group(1).strip() if elf_class else None,
        "readelf_notes_exit": notes.returncode,
    }


def symbol_list(path: Path, output_dir: Path, name: str) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    proc = command("aarch64-linux-gnu-nm", "-a", "--format=posix", str(path), check=False)
    lines = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    (output_dir / f"{name}.nm.txt").write_text(proc.stdout)
    readelf_symbols = command("aarch64-linux-gnu-readelf", "-W", "-s", str(path), check=False)
    (output_dir / f"{name}.readelf.txt").write_text(readelf_symbols.stdout)
    if proc.returncode and not lines:
        return {"available": False, "exit_code": proc.returncode,
                "reason": (proc.stderr or proc.stdout).strip()[:500], "symbols": []}
    names = set()
    for line in lines:
        parts = line.split()
        if parts:
            names.add(parts[0])
    return {"available": True, "exit_code": proc.returncode,
            "readelf_exit_code": readelf_symbols.returncode,
            "symbol_count": len(names), "symbols": sorted(names),
            "full_nm_path": f"symbols/{name}.nm.txt",
            "full_readelf_symbols_path": f"symbols/{name}.readelf.txt"}


def w103d_scan(path: Path, symbols: dict) -> dict:
    symbol_names = set(symbols.get("symbols", []))
    strings = command("strings", "-a", str(path), check=False).stdout.splitlines()
    string_set = set(strings)
    return {
        "symbol_presence": {name: name in symbol_names for name in W103D_MARKERS},
        "literal_presence": {name: name in string_set for name in W103D_MARKERS},
        "scope": "allowlisted identifiers/literals only; absence is inconclusive for stripped/inlined code",
    }


def modprobe_options(raw: Path, root: str, scratch: Path) -> list[dict]:
    target_names = {re.sub(r"[-_]", "", name.lower()) for name in MODULES}
    records = []
    for directory in MODPROBE_DIRS:
        for filename in debugfs_names(raw, directory):
            if not filename.endswith(".conf") or not re.fullmatch(r"[A-Za-z0-9_.+-]+", filename):
                continue
            source = f"{directory}/{filename}"
            local = scratch / "modprobe" / f"{len(records):03d}-{filename}"
            if not dump(raw, source, local):
                continue
            for number, line in enumerate(local.read_text(errors="replace").splitlines(), 1):
                match = re.match(r"\s*(options|blacklist|install|remove|softdep|alias)\s+(\S+)(?:\s+(.*))?$", line)
                if not match:
                    continue
                directive, target, rest = match.groups()
                normalized = re.sub(r"[-_]", "", target.lower())
                # Alias entries can target a module in their final token.
                alias_target = re.sub(r"[-_]", "", (rest or "").split()[-1].lower()) if directive == "alias" and rest else ""
                if normalized in target_names or alias_target in target_names:
                    records.append({"path": source, "line": number, "text": line.strip(),
                                    "directive": directive, "module_target": target})
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--reference-lock", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.reference_lock.read_text())
    components = args.components.resolve(strict=True)
    data = components / "data.PARTITION"
    if not data.is_file():
        raise SystemExit("locked image components lack data.PARTITION")

    args.output_dir.mkdir(parents=True, exist_ok=False)
    module_output = args.output_dir / "modules"
    module_output.mkdir()
    report = {
        "schema": 1,
        "source": {"release_tag": lock["release_tag"], "asset": lock["asset_name"],
                   "zip_sha256": lock["zip_sha256"], "member": lock["member_name"],
                   "image_sha256": lock["image_sha256"]},
        "scope": "Eight allowlisted reference Wi-Fi modules and matching static modprobe settings; no image mounts or execution.",
        "expected_kernel_release": EXPECTED_RELEASE,
        "modules": [],
        "modprobe_static_matches": [],
        "parameter_limits": "modinfo parm entries describe declarations, not compiled defaults or runtime values.",
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="w103d-reference-module-evidence-") as td:
        scratch = Path(td)
        raw = scratch / "rootfs.raw"
        partition_info = raw_partition(data, raw)
        root_raw = raw if partition_info.get("format") == "android_sparse" else data
        root = "/usr/lib/modules"
        releases = debugfs_names(root_raw, root)
        if EXPECTED_RELEASE not in releases:
            root = "/lib/modules"
            releases = debugfs_names(root_raw, root)
        if EXPECTED_RELEASE not in releases:
            raise SystemExit(f"expected module release {EXPECTED_RELEASE} absent from reference rootfs")

        dep_local = scratch / "modules.dep"
        dep_path = f"{root}/{EXPECTED_RELEASE}/modules.dep"
        if not dump(root_raw, dep_path, dep_local):
            raise SystemExit(f"cannot read dependency map {dep_path}")
        dep_map = {}
        for line in dep_local.read_text(errors="replace").splitlines():
            module, sep, deps = line.partition(":")
            if sep:
                dep_map[module.strip()] = [item for item in deps.split() if item.endswith(".ko.xz")]
        missing = [relative for relative in MODULES.values() if relative not in dep_map]
        if missing:
            raise SystemExit("allowlisted module paths absent from modules.dep: " + ", ".join(missing))

        top = MODULES["mt7663s"]
        closure, pending = set(), [top]
        while pending:
            item = pending.pop()
            if item in closure:
                continue
            closure.add(item)
            pending.extend(dep_map.get(item, []))
        report["wireless_dependency_closure"] = sorted(set(MODULES.values()) & closure)
        report["dependency_closure_complete"] = set(MODULES.values()).issubset(closure)
        if not report["dependency_closure_complete"]:
            raise SystemExit("selected wireless module allowlist is not closed under modules.dep")

        for name, relative in MODULES.items():
            compressed = scratch / f"{name}.ko.xz"
            module = module_output / f"{name}.ko"
            dump_module(root_raw, root, EXPECTED_RELEASE, relative, compressed)
            compressed_hash = sha256(compressed)
            decompress_xz(compressed, module)
            symbols = symbol_list(module, args.output_dir / "symbols", name)
            record = {
                "name": name,
                "reference_path": f"{root}/{EXPECTED_RELEASE}/{relative}",
                "compressed_sha256": compressed_hash,
                "uncompressed_sha256": sha256(module),
                "uncompressed_size": module.stat().st_size,
                "elf": elf_metadata(module),
                "modinfo": modinfo_fields(module),
                "symbol_table": symbols,
                "w103d_marker_scan": w103d_scan(module, symbols),
            }
            report["modules"].append(record)

        report["modprobe_static_matches"] = modprobe_options(root_raw, root, scratch)
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"report": str(report_path), "module_count": len(report["modules"]),
                      "dependency_closure_complete": report["dependency_closure_complete"],
                      "errors": report["errors"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
