#!/usr/bin/env python3
"""Read-only audit of an existing W103D kernel artifact for later MT76 module work."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from prepare_kernel_input import verify_inner_checksums

REQUIRED_HEADER_SUFFIXES = (
    "Module.symvers", "include/generated/autoconf.h", "include/config/auto.conf",
    "include/config/kernel.release", "include/generated/utsrelease.h",
    "scripts/Makefile.modpost", "scripts/mod/modpost", "scripts/basic/fixdep", "scripts/module.lds",
)
CONFIG_KEYS = (
    "CONFIG_MODVERSIONS", "CONFIG_MODULE_SIG", "CONFIG_MODULE_SIG_FORCE", "CONFIG_FTRACE",
    "CONFIG_FUNCTION_TRACER", "CONFIG_DEBUG_INFO_BTF", "CONFIG_DEBUG_INFO_BTF_MODULES",
)
MODULES = ("mt7663s", "mt7615_common", "mt76", "mt76_connac_lib", "mt76_connac2", "mac80211", "cfg80211")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fail(msg: str) -> None:
    raise ValueError(msg)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        fail(f"invalid JSON {path.name}: {type(exc).__name__}")
    if not isinstance(value, dict):
        fail(f"expected JSON object: {path.name}")
    return value


def validate_metadata(lock: dict, recovery: dict, recovery_jobs: dict, artifacts: dict,
                      source: dict, source_jobs: dict) -> dict:
    expected = lock["recovery_run"]
    src = lock["source_compile_run"]
    checks = {}
    checks["recovery_run"] = (recovery.get("id") == int(expected["id"])
        and recovery.get("status") == "completed" and recovery.get("conclusion") == "success"
        and recovery.get("path") == expected["workflow_path"] and recovery.get("head_sha") == expected["head_sha"])
    rjob = next((j for j in recovery_jobs.get("jobs", []) if j.get("name") == expected["job_name"]), None)
    checks["recovery_job"] = bool(rjob and rjob.get("conclusion") == "success")
    rsteps = {s.get("name"): s.get("conclusion") for s in (rjob or {}).get("steps", [])}
    checks["recovery_steps"] = all(rsteps.get(name) == "success" for name in (
        "Authenticate recovery to the exact source compile run and failure",
        "Download the already compiled artifact",
        "Revalidate debs and nested Ophub kernel archives without compiling",
        "Upload the verified kernel feed and recovery provenance"))
    matches = [a for a in artifacts.get("artifacts", []) if a.get("name") == expected["artifact_name"]]
    checks["recovery_artifact_live"] = len(matches) == 1 and not matches[0].get("expired", True)
    checks["source_run"] = (source.get("id") == int(src["id"]) and source.get("status") == "completed"
        and source.get("conclusion") == src["expected_run_conclusion"]
        and source.get("path") == src["workflow_path"] and source.get("head_sha") == src["head_sha"])
    cjob = next((j for j in source_jobs.get("jobs", []) if j.get("name") == src["job_name"]), None)
    csteps = {s.get("name"): s.get("conclusion") for s in (cjob or {}).get("steps", [])}
    checks["source_compile_steps"] = bool(cjob and cjob.get("conclusion") == "failure" and all(
        csteps.get(name) == result for name, result in src["step_conclusions"].items()))
    return {"checks": checks, "ok": all(checks.values()),
            "recovery_artifact_id": matches[0].get("id") if matches else None,
            "source_compile_run_id": src["id"]}


def safe_regular_members(bundle: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    files = {}
    for member in bundle.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            fail(f"unsafe archive path: {member.name}")
        if member.isfile():
            files[str(name)] = member
    return files


def exactly_one(paths: list[Path], what: str) -> Path:
    if len(paths) != 1:
        fail(f"expected one {what}, found {len(paths)}")
    return paths[0]


def extract_outer_member(bundle: tarfile.TarFile, members: dict[str, tarfile.TarInfo],
                         name: str, dest: Path) -> None:
    member = members.get(name)
    if member is None or not member.isfile():
        fail(f"missing regular archive member: {name}")
    stream = bundle.extractfile(member)
    if stream is None:
        fail(f"cannot read archive member: {name}")
    with stream, dest.open("wb") as out:
        shutil.copyfileobj(stream, out)


def contents(archive: Path) -> tuple[tarfile.TarFile, dict[str, tarfile.TarInfo]]:
    bundle = tarfile.open(archive, "r:gz")
    return bundle, safe_regular_members(bundle)


def find_suffix(files: dict[str, tarfile.TarInfo], suffix: str) -> list[str]:
    return [name for name in files if name == suffix or name.endswith("/" + suffix)]


def member_bytes(bundle: tarfile.TarFile, files: dict[str, tarfile.TarInfo], name: str,
                 max_bytes: int = 16 * 1024 * 1024) -> bytes:
    member = files.get(name)
    if member is None or member.size > max_bytes:
        fail(f"missing or oversized member: {name}")
    stream = bundle.extractfile(member)
    if stream is None:
        fail(f"cannot read member: {name}")
    return stream.read(max_bytes + 1)


def kconfig_values(bundle: tarfile.TarFile, files: dict[str, tarfile.TarInfo], config_member: str | None,
                   complete_config: bool) -> dict:
    if not config_member:
        return {key: None for key in CONFIG_KEYS}
    raw = member_bytes(bundle, files, config_member).decode("utf-8", "replace")
    out = {}
    for key in CONFIG_KEYS:
        match = re.search(r"(?m)^" + re.escape(key) + r"=(.*)$", raw)
        unset = re.search(r"(?m)^# " + re.escape(key) + r" is not set$", raw)
        out[key] = match.group(1) if match else ("n" if unset else (None if complete_config else "unknown (not in auto.conf)"))
    return out


def modinfo(path: Path, field: str) -> str | None:
    try:
        p = subprocess.run(["modinfo", "-F", field, str(path)], capture_output=True, text=True, timeout=12)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    return p.stdout.strip() or None


def audit_archive(args: argparse.Namespace, metadata: dict, report: dict) -> None:
    lock = read_json(args.lock)
    fixed = lock["kernel_input_manifest"]
    artifact = args.artifact_dir
    prov_path = exactly_one(list(artifact.rglob("recovery-provenance.json")), "recovery-provenance.json")
    input_path = exactly_one(list(artifact.rglob("kernel-input-manifest.json")), "kernel-input-manifest.json")
    provenance, downloaded_manifest = read_json(prov_path), read_json(input_path)
    src = lock["source_compile_run"]
    recovery = lock["recovery_run"]
    lineage = (provenance.get("recovery_run_id") == recovery["id"]
        and provenance.get("source_compile_run_id") == src["id"]
        and provenance.get("source_compile_head_sha") == src["head_sha"]
        and provenance.get("source_compile_workflow_path") == src["workflow_path"]
        and provenance.get("source_compile_step_conclusion") == "success"
        and provenance.get("source_verification_step_conclusion") == "failure"
        and provenance.get("source_failure_signature") == src["failure_signature"])
    report["artifact_provenance_valid"] = lineage
    if not lineage:
        fail("recovery provenance does not match the fixed compile/recovery lineage")
    for key in ("kernel_version", "kernel_release", "archive_sha256", "sources_lock_sha256",
                "kernel_packages_manifest_sha256", "inner_sha256_members"):
        if downloaded_manifest.get(key) != fixed.get(key):
            fail(f"recovery artifact manifest differs from fixed verified value: {key}")
    archive = exactly_one([p for p in artifact.rglob(fixed["archive_artifact_path"]) if p.name == "6.18.54.tar.gz"],
                          "6.18.54.tar.gz")
    if sha_file(archive) != fixed["archive_sha256"]:
        fail("runtime archive SHA-256 differs from verified report")
    sources_lock = exactly_one(list(artifact.rglob("sources.lock.json")), "sources.lock.json")
    package_manifest = exactly_one(list(artifact.rglob("kernel-packages.json")), "kernel-packages.json")
    if sha_file(sources_lock) != fixed["sources_lock_sha256"]:
        fail("sources.lock.json SHA-256 mismatch")
    if sha_file(package_manifest) != fixed["kernel_packages_manifest_sha256"]:
        fail("kernel-packages.json SHA-256 mismatch")
    toolchain = list(artifact.rglob("toolchain-version.txt"))
    report["toolchain_version_observed"] = toolchain[0].read_text(errors="replace")[:3000].strip() if len(toolchain) == 1 else None
    report["toolchain_lock"] = lock["kernel_input_manifest"]["ophub_source_lock"].get("kernel_toolchain")
    if not report["toolchain_version_observed"]:
        report["errors"].append("toolchain-version.txt missing or ambiguous")

    with tarfile.open(archive, "r:gz") as outer:
        actual_inner = verify_inner_checksums(archive, fixed["kernel_version"])
        report["inner_hashes_match"] = actual_inner == fixed["inner_sha256_members"]
        if not report["inner_hashes_match"]:
            fail("nested package archive hashes do not match the verified report")
        outer_files = safe_regular_members(outer)
        member_names = fixed["inner_sha256_members"]
        header_name = exactly_one([n for n in member_names if PurePosixPath(n).name.startswith("header-")], "header archive member")
        modules_name = exactly_one([n for n in member_names if PurePosixPath(n).name.startswith("modules-")], "modules archive member")
        boot_name = exactly_one([n for n in member_names if PurePosixPath(n).name.startswith("boot-")], "boot archive member")
        with tempfile.TemporaryDirectory(prefix="wifi-module-preflight-") as tmp:
            temp = Path(tmp)
            nested = {}
            for label, name in (("headers", header_name), ("modules", modules_name), ("boot", boot_name)):
                target = temp / PurePosixPath(name).name
                extract_outer_member(outer, outer_files, name, target)
                nested[label] = target
            header_bundle, header_files = contents(nested["headers"])
            module_bundle, module_files = contents(nested["modules"])
            boot_bundle, boot_files = contents(nested["boot"])
            try:
                header_checks = {suffix: find_suffix(header_files, suffix) for suffix in REQUIRED_HEADER_SUFFIXES}
                report["header_files"] = {k: (v[0] if len(v) == 1 else None) for k,v in header_checks.items()}
                release_members = find_suffix(header_files, "include/config/kernel.release")
                release = member_bytes(header_bundle, header_files, release_members[0]).decode().strip() if len(release_members)==1 else None
                report["header_kernel_release"] = release
                report["header_input_files_present"] = all(len(v)==1 for v in header_checks.values())
                report["header_release_matches"] = release == fixed["kernel_release"]
                if not report["header_input_files_present"]: report["input_issues"].append("one or more prepared-header inputs are missing or ambiguous")
                if not report["header_release_matches"]: report["input_issues"].append("header kernel.release does not match expected release")
                symvers = header_checks["Module.symvers"]
                symvers_bytes = 0
                if len(symvers)==1:
                    data=member_bytes(header_bundle,header_files,symvers[0],64*1024*1024)
                    symvers_bytes=len(data)
                    report["Module.symvers_sha256"]=sha_bytes(data)
                    report["Module.symvers_bytes"]=symvers_bytes
                if symvers_bytes == 0: report["input_issues"].append("Module.symvers missing or empty")
                full_configs = find_suffix(header_files, ".config")
                boot_configs = [n for n in boot_files if re.search(r"(?:^|/)config-"+re.escape(fixed["kernel_release"])+r"$", n)]
                auto_configs = find_suffix(header_files, "include/config/auto.conf")
                if len(full_configs)==1:
                    config_source, config_bundle, config_files, config_path, complete_config = "headers/.config",header_bundle,header_files,full_configs[0],True
                elif len(boot_configs)==1:
                    config_source, config_bundle, config_files, config_path, complete_config = "boot config",boot_bundle,boot_files,boot_configs[0],True
                elif len(auto_configs)==1:
                    config_source, config_bundle, config_files, config_path, complete_config = "headers/auto.conf",header_bundle,header_files,auto_configs[0],False
                else:
                    config_source, config_bundle, config_files, config_path, complete_config = None,header_bundle,header_files,None,False
                report["kconfig_source"] = config_source
                report["kconfig"] = kconfig_values(config_bundle, config_files, config_path, complete_config)
                if report["kconfig"].get("CONFIG_MODULE_SIG") == "n":
                    # Locked kernel/module/Kconfig makes MODULE_SIG_FORCE depend on MODULE_SIG.
                    report["kconfig"]["CONFIG_MODULE_SIG_FORCE"] = "not applicable: CONFIG_MODULE_SIG=n"
                report["kconfig_applicability"] = {
                    "CONFIG_MODULE_SIG_FORCE": {
                        "depends_on": "CONFIG_MODULE_SIG",
                        "source_url": "https://raw.githubusercontent.com/ophub/linux-6.18.y/0f189d6b3197b94a8fbc96a670f0095cd63ce1a9/kernel/module/Kconfig",
                        "interpretation": "not applicable when CONFIG_MODULE_SIG=n; otherwise an absent value remains unknown",
                    }
                }
                report["kconfig_unknown"] = [k for k,v in report["kconfig"].items()
                    if v is None or str(v).startswith("unknown")]
                module_dep = find_suffix(module_files, "modules.dep")
                if len(module_dep) != 1:
                    report["errors"].append("modules.dep missing or ambiguous")
                    dep_paths=[]
                else:
                    dep_paths=[line.split(":",1)[0] for line in member_bytes(module_bundle,module_files,module_dep[0]).decode("utf-8","replace").splitlines() if ":" in line]
                report["module_path_count"]=len(dep_paths)
                selected=[]
                for name in MODULES:
                    # Module names in module tooling normalize '-' and '_' to the same name.
                    archive_basename = re.escape(name).replace("_", "[-_]")
                    candidates=[p for p in dep_paths if re.search(r"(?:^|/)"+archive_basename+r"\.ko(?:\.(?:xz|zst|gz))?$",p)]
                    if len(candidates)>1:
                        report["errors"].append(f"module path ambiguous: {name}");continue
                    if not candidates:
                        selected.append({"name":name,"present":False});continue
                    path=candidates[0]
                    archive_paths=[p for p in module_files if PurePosixPath(p).as_posix().endswith(path)]
                    if len(archive_paths)!=1:
                        report["errors"].append(f"module payload missing or ambiguous: {name}");continue
                    info=module_bundle.getmember(archive_paths[0])
                    stream=module_bundle.extractfile(info)
                    module_tmp=temp/Path(PurePosixPath(path).name)
                    if stream is None: report["errors"].append(f"cannot read module payload: {name}");continue
                    with stream,module_tmp.open("wb") as out:shutil.copyfileobj(stream,out)
                    selected.append({"name":name,"path":path,"archive_sha256":sha_file(module_tmp),
                        "bytes":info.size,"vermagic":modinfo(module_tmp,"vermagic"),"srcversion":modinfo(module_tmp,"srcversion")})
                report["modules"]=selected
                required_modules={m["name"]:m for m in selected}
                vermagic_modules = ("mt7663s", "mt7615_common", "mt76", "mac80211", "cfg80211")
                for name in vermagic_modules:
                    module=required_modules.get(name)
                    if not module or not module.get("vermagic"):
                        report["input_issues"].append(f"required module vermagic unavailable: {name}")
                    elif module["vermagic"].split()[0] != fixed["kernel_release"]:
                        report["input_issues"].append(f"required module vermagic release mismatch: {name}")
                for name in ("mt7663s", "mt7615_common"):
                    if not required_modules.get(name, {}).get("archive_sha256"):
                        report["input_issues"].append(f"target driver module/hash unavailable: {name}")
                if shutil.which("modinfo") is None:
                    report["errors"].append("modinfo unavailable; module vermagic/srcversion not verified")
                if report["kconfig_unknown"]:
                    report["input_issues"].append("required Kconfig values are unknown: " + ", ".join(report["kconfig_unknown"]))
                report["inputs_ready"] = (report["header_input_files_present"] and report["header_release_matches"]
                    and symvers_bytes > 0 and not report["input_issues"])
                report["kbuild_compatibility"]={"candidate_inputs_present":report["inputs_ready"],
                    "kernel_release_matches":report["header_release_matches"],"Module.symvers_nonempty":symvers_bytes>0,
                    "config_modversions":report.get("kconfig",{}).get("CONFIG_MODVERSIONS"),
                    "module_vermagic_matches_release":all(required_modules.get(n,{}).get("vermagic","").split()[:1]==[fixed["kernel_release"]] for n in vermagic_modules)}
                report["limitations"]=["Static archive inspection only; a successful external-module kbuild has not been attempted."]
            finally:
                header_bundle.close();module_bundle.close();boot_bundle.close()
    report["metadata_complete"] = True
    report["audit_complete"] = not report["errors"]


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--phase",choices=("metadata","audit"),required=True)
    parser.add_argument("--lock",type=Path,required=True)
    parser.add_argument("--recovery-run",type=Path,required=True)
    parser.add_argument("--recovery-jobs",type=Path,required=True)
    parser.add_argument("--recovery-artifacts",type=Path,required=True)
    parser.add_argument("--source-run",type=Path,required=True)
    parser.add_argument("--source-jobs",type=Path,required=True)
    parser.add_argument("--artifact-dir",type=Path)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    report={"metadata_complete":False,"audit_complete":False,"inputs_ready":False,"errors":[],"input_issues":[]}
    try:
        lock=read_json(args.lock)
        meta=validate_metadata(lock,read_json(args.recovery_run),read_json(args.recovery_jobs),
            read_json(args.recovery_artifacts),read_json(args.source_run),read_json(args.source_jobs))
        report.update(metadata_validation=meta)
        if not meta["ok"]:
            report["errors"].append("run lineage, required steps, or live artifact validation failed")
        else:
            report["metadata_complete"]=True
            if args.phase=="audit":
                if args.artifact_dir is None: fail("--artifact-dir is required for audit phase")
                audit_archive(args,meta,report)
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {str(exc)[:500]}")
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps({"metadata_complete":report["metadata_complete"],"audit_complete":report["audit_complete"],
                      "inputs_ready":report["inputs_ready"],"errors":report["errors"]},ensure_ascii=False))
    complete = report["metadata_complete"] if args.phase=="metadata" else report["audit_complete"]
    return 0 if complete else 1

if __name__=="__main__":
    raise SystemExit(main())
