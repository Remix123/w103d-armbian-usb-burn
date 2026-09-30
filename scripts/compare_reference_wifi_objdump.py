#!/usr/bin/env python3
"""Disassemble a small allowlist of Wi-Fi functions from two locked artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import re


REFERENCE_RUN = "36789346056"
REFERENCE_HEAD = "c8abd678052adb814647cb07886b35eabdc2794f"
REFERENCE_ARTIFACT = f"reference-wifi-module-evidence-{REFERENCE_RUN}"
REFERENCE_ZIP_SHA256 = "7db21d4bba6e13a2176eed81894433f2dadd8acf1c4a0cd6bac9f64797a47dc5"
REFERENCE_IMAGE_SHA256 = "60b0c3355eda080cdf0c350e063bb6c8ac664f0dea872f5d5377d8a53503159d"
CURRENT_RUN = "36784582315"
CURRENT_HEAD = "1059fe6393858b0ca1b7c3c6c061faae0bcff14b"
CURRENT_ARTIFACT = f"w103d-mt76-preassoc-rlm-optional-ctx-candidate-{CURRENT_RUN}"
CURRENT_SOURCE_COMMIT = "0f189d6b3197b94a8fbc96a670f0095cd63ce1a9"
CURRENT_PATCH_SHA256 = "082577ad5a1ee00cd9f04956ba6e51654603db03c79028e8ea1fe2ae6f1caa5e"
REFERENCE_MODULE_SHA256 = {
    "mt7615-common": "01f6c7c9e55705e267c79fbe1c63a337a4789274d174d0cc461cfd831b18cbe7",
    "mt7663s": "b7f90324d651a45b0ddcdb997520aa98e41a1d52eac1d2501359126221204c57",
}
CURRENT_MODULE_SHA256 = {
    "mt7615-common": "295da3113633027e86091e78e8ec789199019a150c2f934e7d3d5b0f70222cc8",
    "mt7663s": "8bea2fab171a44c356f8eb5f3a54bf0c9ea3b2ef3d210fa51804853c261a8322",
}

# Functions on the unaffiliated transmit/channel/MCU paths.  The RLM builder
# was split/renamed in the current build; symbol absence can mean inlining.
FUNCTIONS = {
    "mt7615-common": ("mt7615_mac_write_txwi",),
    "mt7663s": (
        "mt7663s_w103d_set_channel",
        "mt7663s_w103d_tx_prepare_skb",
        "mt7663s_w103d_mcu_send_msg",
        "mt7663s_w103d_add_dev_info",
        "mt7663s_w103d_fill_bss_rlm",
        "mt7663s_w103d_sync_bss_rlm",
    ),
}
MODULES = ("mt7615-common", "mt7663s")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def command(*argv: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result


def module_map(records: list[dict], key: str) -> dict[str, dict]:
    return {record[key]: record for record in records}


def instruction_lines(disassembly: str) -> list[str]:
    return [line for line in disassembly.splitlines()
            if re.match(r"^\s*[0-9a-fA-F]+:\s+(?:[0-9a-fA-F]{2,8}\s+)+[A-Za-z.][A-Za-z0-9_.]*", line)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    ref_report = read_json(args.reference / "report.json")
    cur_manifest = read_json(args.current / "manifest.json")
    assert ref_report["source"]["zip_sha256"] == REFERENCE_ZIP_SHA256
    assert ref_report["source"]["image_sha256"] == REFERENCE_IMAGE_SHA256
    assert ref_report["dependency_closure_complete"] is True
    assert ref_report["errors"] == []
    assert cur_manifest["source"]["commit"] == CURRENT_SOURCE_COMMIT
    assert cur_manifest["diagnostic_patch"]["sha256"] == CURRENT_PATCH_SHA256
    assert cur_manifest["functional_candidate"] == "preassoc-home-rlm-optional-ctx"
    assert cur_manifest["build_complete"] is True and cur_manifest["candidate_build_complete"] is True
    assert cur_manifest["errors"] == []

    ref_records = module_map(ref_report["modules"], "name")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": 1,
        "provenance": {
            "reference_run_id": REFERENCE_RUN, "reference_head": REFERENCE_HEAD,
            "reference_artifact": REFERENCE_ARTIFACT,
            "reference_zip_sha256": REFERENCE_ZIP_SHA256,
            "reference_image_sha256": REFERENCE_IMAGE_SHA256,
            "current_run_id": CURRENT_RUN, "current_head": CURRENT_HEAD,
            "current_artifact": CURRENT_ARTIFACT,
            "current_source_commit": CURRENT_SOURCE_COMMIT,
            "current_patch_sha256": CURRENT_PATCH_SHA256,
        },
        "method": "AArch64 objdump -dr --disassemble=<allowlisted-symbol>; no module is executed or loaded.",
        "limits": [
            "Reference and current artifacts were built for different kernel releases and with different compiler versions; instruction layout/register allocation differences alone are not behavioral proof.",
            "A missing standalone symbol may be inlined, renamed, or optimized; it is not evidence that the behavior is absent.",
            "Disassembly is static evidence only and cannot prove firmware acceptance, over-the-air reception, or runtime module parameters.",
        ],
        "modules": [],
    }

    for module_name in MODULES:
        reference_path = args.reference / "modules" / f"{module_name.replace('-', '_')}.ko"
        current_path = args.current / f"{module_name}.ko"
        expected_ref = REFERENCE_MODULE_SHA256[module_name]
        expected_cur = CURRENT_MODULE_SHA256[module_name]
        assert sha256(reference_path) == expected_ref, f"reference {module_name} SHA mismatch"
        assert sha256(current_path) == expected_cur, f"current {module_name} SHA mismatch"

        ref_nm = command("aarch64-linux-gnu-nm", "-a", "--format=posix", str(reference_path)).stdout
        cur_nm = command("aarch64-linux-gnu-nm", "-a", "--format=posix", str(current_path)).stdout
        ref_symbols = {line.split()[0] for line in ref_nm.splitlines() if line.split()}
        cur_symbols = {line.split()[0] for line in cur_nm.splitlines() if line.split()}
        (args.output_dir / f"{module_name}-reference.nm.txt").write_text(ref_nm)
        (args.output_dir / f"{module_name}-current.nm.txt").write_text(cur_nm)
        entries = []
        for symbol in FUNCTIONS[module_name]:
            entry = {"symbol": symbol, "reference_present": symbol in ref_symbols,
                     "current_present": symbol in cur_symbols}
            for side, path, present in (("reference", reference_path, symbol in ref_symbols),
                                        ("current", current_path, symbol in cur_symbols)):
                if not present:
                    entry[f"{side}_status"] = "no standalone nm symbol; may be inlined/renamed"
                    continue
                disassembly = command("aarch64-linux-gnu-objdump", "-dr",
                                      f"--disassemble={symbol}", str(path)).stdout
                sidecar = args.output_dir / f"{module_name}-{side}-{symbol}.objdump.txt"
                sidecar.write_text(disassembly)
                header = re.search(r"<" + re.escape(symbol) + r">:", disassembly)
                instructions = instruction_lines(disassembly)
                if not header or not instructions:
                    raise RuntimeError(
                        f"objdump for {module_name}/{side}/{symbol} returned no symbol body/instruction; "
                        f"saved raw output at {sidecar}"
                    )
                entry[f"{side}_status"] = "disassembled"
                entry[f"{side}_sidecar"] = sidecar.name
                entry[f"{side}_bytes"] = sidecar.stat().st_size
                entry[f"{side}_instruction_lines"] = len(instructions)
            entries.append(entry)

        if not any(item["reference_present"] for item in entries):
            raise RuntimeError(f"no selected reference symbols available in {module_name}; refusing empty comparison")
        if not any(item["current_present"] for item in entries):
            raise RuntimeError(f"no selected current symbols available in {module_name}; refusing empty comparison")

        report["modules"].append({
            "name": module_name,
            "reference_sha256": expected_ref,
            "current_sha256": expected_cur,
            "reference_vermagic": ref_records[module_name.replace("-", "_")]["modinfo"]["vermagic"]["values"],
            "current_vermagic": next(x["vermagic"] for x in cur_manifest["modules"]
                                      if x["name"] == module_name),
            "symbols": entries,
        })

    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output_dir), "modules": len(report["modules"]),
                      "functions": sum(len(m["symbols"]) for m in report["modules"]),
                      "available_symbols": sum(sum(bool(s["reference_present"]) + bool(s["current_present"])
                                                    for s in m["symbols"]) for m in report["modules"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
