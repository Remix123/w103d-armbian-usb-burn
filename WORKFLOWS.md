# W103D 两种驱动的构建入口

两个入口均构建 Ophub 当前支持的 W103D 内核，以及 Trixie/server/ext4 系统。

| Actions 名称 | 文件 | 驱动选择 |
|---|---|---|
| Build and deliver W103D USB image | `.github/workflows/build-and-deliver.yml` | `config/kernel-patches.json`，应用本仓库 v23 补丁 |
| Build and deliver W103D USB image (Stock 2021 driver) | `.github/workflows/build-usb-stock-driver.yml` | `config/kernel-patches-stock.json`，空补丁列表，使用锁定版本的 Ophub 原版驱动 |

Stock 名称中的 2021 指两版共同使用的 N9 固件，不代表回退到一份 2021 年 Linux 驱动源码。两版固件均受 `config/wifi-firmware.lock.json` 约束。入口名称为兼容既有 Actions 操作习惯保留。

## 编排及恢复

两个入口均运行 `scripts/orchestrate_build.py`，顺序为：

1. `reference-preflight.yml`：验证参考包。
2. `compile-kernel.yml`：解析并固定源码、配置、工具链、容器和所选补丁；预检后编译/复用完整缓存。
3. `build-system.yml`：使用明确的 `kernel_run_id` 消费内核 artifact，重建最新发布的官方 Trixie 基础系统。
4. `build-usb-burn-image.yml`：使用明确的 `system_run_id` 组装、独立回读验证 USB IMG。
5. `deliver-zipped-image.yml`：使用明确的 `usb_run_id` 生成并回读验证最终 ZIP。
6. 父工作流验证 ZIP 后发布对应驱动的 Release。

每个子运行通过 workflow 路径、触发分支、HEAD SHA 和 `pipeline-<run-id>-<attempt>` 共同关联。不能用最近一次运行代替本流水线子运行。父流程失败或取消后，只清理自己关联的子运行。两个入口共享父流程并发组，避免同时占用整套构建资源。

Stock 的 `run_all_jobs` 默认 true；设为 false 会跳过整个作业。两个入口都支持 `reuse_system_run_id` 或 `reuse_usb_run_id`，二选一。复用必须是成功且来源合法的运行，后续阶段还会检查其 source lock 的驱动类型和补丁哈希，禁止 Stock/v23 串用或复用已变化的补丁。

父流程总预算为 350 分钟，作业上限 360 分钟。它是所有阶段共享的总预算，不是每阶段均可额外使用 350 分钟。

## 缓存

缓存仅允许完整精确命中；不再使用 `w103d-kernel-` 前缀恢复其他构建的产物。

`kernel_cache.py` 的键包含：

- 固定的 Ophub 构建源码、内核源码、内核配置仓库/路径/内容哈希。
- 所选补丁配置、实际补丁哈希，以及工具链和容器摘要。
- 编译工作流和参与产物生成、验证的本仓库脚本内容。

缓存含 DEB、`kernel-packages.json`、`sources.lock.json`、工具链版本和完整运行时 tarball。命中后仍校验当前 source lock、DEB 和运行时 archive 的哈希及内部校验清单。残缺缓存必须失败，不能宣称构建成功。新键使用 `w103d-kernel-v2-`，旧的不完整缓存自然失效。

## 产物与在线内核源隔离

IMG/ZIP 继续采用原版本化文件名，以保持所有交付验证器的接口一致。两版通过 source lock、独立运行 ID、Release 标签和标题区分。

- v23 内核源：Release `kernel_stable_v23`，系统 `KERNEL_TAGS=stable_v23`。
- Stock 内核源：Release `kernel_stable_stock`，系统 `KERNEL_TAGS=stable_stock`。
- 两个内核源发布 runtime tar 包，镜像的 `DOWNLOAD_TYPE` 设置并回读验证为 `tar`，避免默认 deb 模式查找未发布的 deb 包。
- 成品标签前缀分别为 `w103d-usb-v23-` 和 `w103d-usb-stock-`，包含父运行及 attempt 标识。
- 成品发布只在新资产回读校验后清理同类型旧版本；不会清理另一类型、旧的不带类型标签、内核源或参考包。

这样同一内核版本的不同驱动不会互相覆盖在线更新源。此隔离逻辑已经与当前锁定 Ophub 源码接口核对；完整重建和设备上的在线升级仍须实际执行验收。

## 本地编排回归检查

Python 3.11+，测试环境安装 PyYAML；生产构建辅助脚本仍只依赖标准库及工作流安装的系统工具。

```bash
python3 -m venv /tmp/w103d-check
/tmp/w103d-check/bin/pip install PyYAML
/tmp/w103d-check/bin/python -m unittest discover -s tests -v
```

审计证据、实际失败运行和修复边界见 `docs/workflow-audit-2026-10-16.md`。这些测试不编译内核、不执行真实镜像挂载、不触发 Actions 或发布 Release。

## initramfs 与 rootfs 固件一致性

两个入口的 USB 组装阶段共用 `initramfs_firmware.py`：先安装并核对 rootfs 的锁定 MT7663 固件，再解开启动用 initramfs 的 early/main CPIO 层。旧版、缺失、压缩或符号链接固件副本不一致时，用 rootfs 中已核对的文件修正并重建归档；early 层保持未压缩及原顺序，main 使用 gzip，因此要求目标内核启用 `CONFIG_RD_GZIP=y`。启动脚本、模块、权限、硬链接和符号链接由 GNU cpio 保留。

整个过程不需要 ARM chroot，不执行内核包 postinst 或 bootloader 更新钩子。`initrd.img-<release>`、`uInitrd` 和 `uInitrd-<release>` 同步更新，原输入镜像不修改。组装报告记录重建前后固件哈希、重建原因和最终 initrd payload 哈希。

最终 verifier 从实际 Amlogic 镜像回读 rootfs 和 FAT/uInitrd，重新解包 initramfs，检查所有固件副本与锁定哈希一致，并核对版本化 initrd/uInitrd、报告和实际 payload。`.github/workflows/check-initramfs-firmware.yml` 提供真实 Linux CPIO、早期归档、硬链接/权限保留及 U-Boot CRC 的小型回归检查，不会触发完整内核构建或发布镜像。
