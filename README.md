# W103D Armbian USB Burning Tool image

Builds a single Amlogic USB Burning Tool `.img` entirely in GitHub Actions. The workflow compiles the current Ophub-supported W103D kernel from pinned source, rebuilds a Trixie userspace image with the exact kernel archive, retains the board-specific payloads from the user-provided reference image, and independently unpacks and validates the final container.

The reference image is stored only as a one-member ZIP asset in the dedicated GitHub Release. GitHub Actions validates both ZIP and IMG hashes before any image inspection. Linux image analysis, loop mounting, filesystem mutation, packing, and verification all run on GitHub-hosted Linux runners. Windows is only used to import and flash the final IMG with USB Burning Tool; it is not a build platform.

The final USB workflow is `.github/workflows/build-usb-burn-image.yml`. Run order, parameters, source locks, recovery rules, partition and boot-chain notes, validation criteria, and the boundary between offline validation and hardware acceptance are documented in [the full Chinese runbook](docs/W103D_USB_Burning_Tool_打包完整手册.md).

Current workflow status is visible in [GitHub Actions](https://github.com/Remix123/w103d-armbian-usb-burn/actions). A successful Actions result proves offline image validation only; USB Burning Tool import, device flashing, and W103D boot acceptance require separate hardware testing.
