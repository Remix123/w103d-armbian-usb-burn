#!/usr/bin/env python3
"""Patch the pinned Ophub wrapper to run its pinned in-checkout kernel helper."""
from pathlib import Path
import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ophub", type=Path, required=True)
    args = ap.parse_args()
    repo = args.ophub.resolve(strict=True)
    wrapper = repo / "recompile"
    helper = repo / "compile-kernel/tools/script/armbian_compile_kernel.sh"
    if not wrapper.is_file() or not helper.is_file():
        raise SystemExit("pinned Ophub checkout is missing recompile or kernel helper")
    source = wrapper.read_text()
    old = '    docker exec -i "${docker_container}" bash "${docker_script}" -u'
    new = ('    docker cp "${current_path}/compile-kernel/tools/script/armbian_compile_kernel.sh" "${docker_container}:${docker_script}"\n'
           '    docker exec "${docker_container}" chmod 0755 "${docker_script}"')
    if source.count(old) != 1:
        raise SystemExit("pinned recompile wrapper no longer has the expected update call; inspect before adapting")
    wrapper.write_text(source.replace(old, new))
    helper_source = helper.read_text()
    old_toolchain = 'toolchain_path="/usr/local/toolchain"'
    new_toolchain = 'toolchain_path="/opt/kernel/compile-kernel/tools/toolchain"'
    if helper_source.count(old_toolchain) != 1:
        raise SystemExit("pinned kernel helper no longer has the expected toolchain path; inspect before adapting")
    helper.write_text(helper_source.replace(old_toolchain, new_toolchain))
    print("Pinned recompile to the checked-out kernel helper and the hash-verified mounted toolchain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
