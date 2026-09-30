# W103D MT7663 N9 firmware A/B plan

This is a review plan only. No device write, firmware replacement, service restart, controller reset, or connection attempt is part of this document's preparation.

## Candidate and selector checks

The reference-preflight firmware artifact contains only these hash-verified files:

| File | Reference SHA-256 | Use |
|---|---|---|
| `mt7663_n9_v3.bin` | `223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a` | Wi-Fi N9 candidate for the offload ROM path |
| `mt7663pr2h.bin` | `534f2152f9b0f48dfcec9c1727b0bab9a3727a655d76d5ec818cc39a55602336` | Shared Bluetooth firmware and Wi-Fi offload ROM patch; do not replace during this A/B |

The A image is the current `/usr/lib/firmware/mediatek/mt7663_n9_v3.bin`, whose recorded SHA-256 is `d8b8488f37f65e41ded9a911950afb000d236b36e5aa94ad9650ad0c3a86fd45`. The B image is the reference N9 v3 file above. Do not use a rebb candidate in this experiment.

Before starting, verify live `prefer_offload_fw=Y`, the selected primary ROM path and hash, the N9 source path/symlink, and current N9 hash. Standard kernel logs may show only the N9 header, not its filename; treat the path as inferred from the runtime selector and driver request order, cross-check the N9 header, and record that limitation. If a firmware-class tracepoint is available, use it read-only or enable it briefly to confirm the request path. Abort if selector, primary ROM, or N9 source cannot be established. Read active initramfs contents without changing it; stop if it supplies an alternate N9 or changes the request source, because this plan does not rebuild initramfs.

## Recovery preparation

Keep the SSH session on wired `eth1` and verify its peer route/default route before any change. Confirm `/` remains on `/dev/mmcblk2p2` via `ffe07000.mmc`; the Wi-Fi controller is `ffe05000.mmc`. Never unbind the root-storage controller, unload the Wi-Fi kernel module, change boot files, alter NV/calibration, or replace the shared `mt7663pr2h.bin` patch.

Record the N9 file's requested path, each symlink in its path, final resolved path, bytes, SHA-256, owner, mode, and timestamps. Back up the resolved regular-file contents and metadata outside the firmware directory. Preserve the symlink itself; replace only its final target and restore the original target bytes and metadata on rollback.

Prepare a rollback script that verifies its backup hash before restoration, atomically restores the original N9 file, verifies the restored hash and symlink chain, and writes a success marker. Before arming a live rollback timer, test the script and timer against a disposable file on the same filesystem: schedule the canary timer, wait for the service to run, confirm its journal/service result and marker, and verify the canary file is restored byte-for-byte. If that timed test does not fire and restore successfully, abort without touching the real firmware. For the actual B window, arm a short one-shot rollback before replacement and verify it is active; cancel it only after all acceptance checks pass. A timer being merely present is not a recovery test.

## Controlled B test

Preserve the wired route, NetworkManager state, DNS, and all existing connection profiles. Keep Wi-Fi autoconnect disabled during the test. Replace only the resolved N9 v3 target with the hash-verified reference file, verify its SHA-256, then rebind only the Wi-Fi controller `ffe05000.mmc` so the driver reloads the N9 firmware. Do not run a whole-machine reboot, module reload, or Bluetooth firmware change.

Capture the N9 version/build log after controller rebind and confirm that the driver requested the N9 v3 path. Then scan only channel 40 / 5200 MHz and confirm the target BSSID `38:f6:cf:25:e3:55` advertises SAE and MFPR. Use a temporary, non-persistent WPA3-SAE profile bound to `wlan0`, with PMF required, the target BSSID, and frequency 5200. Set IPv4/IPv6 never-default and prevent Wi-Fi DNS or route changes. Supply the password interactively through a no-echo mechanism; do not put it in a command line, file, report, or persistent profile.

The test passes only if authentication proceeds to association, the interface receives an address, `wlan0`-bound traffic succeeds (for example, a ping to the LAN gateway with `-I wlan0`), and the wired SSH route, default route, DNS, and existing profiles remain intact. A scan result or firmware boot log alone is not a pass. The last controlled attempt reached the target AP during a single-channel scan but timed out at SAE authentication (`AUTH_TIMED_OUT`), before association or DHCP; it did not reproduce the RX error storm.

## Rollback and cleanup

On authentication timeout, missing N9 log, controller error, wired-route change, or any acceptance failure, let the tested rollback path restore the original N9 blob and verify its original hash and symlink chain. Rebind only `ffe05000.mmc` to reload the original blob, then verify wired SSH route/default route, DNS, and NetworkManager state. Do not repeat the B attempt in the same window.

On B pass, record the association, address, interface-bound ping, and preserved wired-route evidence, then restore A and repeat the same channel-40 connection check as a minimal A-B-A confirmation. Cancel the live rollback timer only after A is restored and verified. In either case, remove the temporary Wi-Fi profile and test-only secret material from memory/tmpfs, remove temporary files/logging overrides, restore the original Wi-Fi autoconnect state, and verify no temporary profile or secret remains. Do not save the supplied WLAN password.

The comparison remains diagnostic: different N9 hashes do not prove the hash difference caused the authentication timeout. The test changes only the N9 firmware while preserving the ROM patch, kernel, module, DTB, and NV state, so it isolates the N9 payload as the single A/B variable.
