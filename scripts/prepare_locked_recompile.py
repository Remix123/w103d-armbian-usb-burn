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
    source = source.replace(old, new)
    # The helper assumes its working directory is /opt/kernel. Docker's default
    # is /, which makes relative config/source paths resolve outside the mount.
    source = source.replace('docker exec -i "${docker_container}"',
                            'docker exec -i -w /opt/kernel "${docker_container}"')
    # The replacement above also needs to cover the one non-interactive chmod
    # command inserted above; keep its explicit chmod call working unchanged.
    if source.count('docker exec -i -w /opt/kernel "${docker_container}"') < 3:
        raise SystemExit("expected all helper docker exec calls to be patched with /opt/kernel working directory")
    if "set -e" not in source.splitlines()[1:6]:
        source = source.replace("#!/bin/bash\n", "#!/bin/bash\nset -e\n", 1)
    wrapper.write_text(source)
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
