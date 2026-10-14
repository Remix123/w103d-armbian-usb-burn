# W103D Armbian USB Burning Tool 镜像构建与验收手册

**适用对象：ZTE W103D / Amlogic S905L3A。** 固件解包、内核编译、Trixie 系统重建、USB Burning Tool 镜像封装、ZIP 压缩和回读验证均在 GitHub Actions/Linux runner 完成。常规用户只 dispatch 一个入口；编排器顺序创建多个独立子 workflow runs。最终下载件是单 ZIP，里面恰有一个可直接导入 USB Burning Tool 的 raw IMG、可用 `sha256sum -c` 验证的 `SHA256SUMS` 以及 provenance/验证报告。ZIP 本身不能导入烧录工具。

本流程以 [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian) 当前支持 W103D 的源代码和内核为系统来源。参考分支 [pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18](https://github.com/pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18) 仅用于理解板级 USB 烧录打包方法；不得从该分支替换系统或内核来源。

> 结果边界：GitHub 工作流只能给出来源可追溯的离线镜像和结构验证。没有实际用 USB Burning Tool 导入、没有刷写 W103D、没有串口冷启动证据时，不能宣称硬件适配成功。

## 1. 当前执行状态

仓库：[Remix123/w103d-armbian-usb-burn](https://github.com/Remix123/w103d-armbian-usb-burn)。

- 最近一次完整的一键构建 [36696946381](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36696946381)（2026-09-30）成功，产出 `W103D_Armbian_26.11.0_6.18.54_USB_Burning_Tool`。该次产出的系统已在实体 W103D 上从 eMMC 启动运行；当时尚未包含 Wi-Fi 补丁和 2021 版固件。
- 2026-10 起，内核编译会应用 `patches/w103d/` 中的 mt7663s 补丁（见 §3.4），USB 镜像改用 2021 版 MT7663 N9 固件（见 §5.2），成品发布到 GitHub Release（只保留最新一个，见 §4.5）。这些改动需要下一次一键构建来产出新的 Release。
- 补丁 v22 + 2021 固件已在实体 W103D（6.18.54-ophub）上做过空闲、已连接扫描（两频段）、5G/2.4G 互切和重载回归；v22 在 2020 固件（3.1.1）上保持 v18 行为。新内核版本（如 6.18.56）的首个镜像仍建议上板做一次 Wi-Fi 冒烟测试。

Amlogic packer 在 [toolchain.lock.json](../config/toolchain.lock.json) 中固定：Khadas `utils` commit `a3604ed6d6863d1946d26d0ac8e765aef6f17430`，`aml_image_v2_packer` SHA-256 `8123b1295abb3262c76b650ba024975e38aedfd49b19f59a09f5920738ef1597`。它是独立的 Amlogic 容器格式打包工具，不是系统或内核源码；需 GitHub x86_64 Linux runner 的 IA32 执行支持。Pigeon W103D burn 分支仅作打包流程参考。USB job 的真实 packer `-c`、`-d`、`-r` 结果由 GitHub run 日志证实；本地 macOS 不运行该工具。

### 3.3 目录和脚本职责

- `.github/workflows/reference-preflight.yml`：参考 ZIP 云端解压、Amlogic packer 预检、镜像布局/DTB/boot 链分析。
- `.github/workflows/compile-kernel.yml`：源解析、W103D 补丁预检与 arm64 内核编译；不会在系统重建时重新取 floating kernel。
- `.github/workflows/build-system.yml`：采用官方通用 Trixie base，经锁定 Ophub source Action 针对 W103D 重建，强制消费 kernel archive 并比较实际镜像内容。
- `.github/workflows/build-usb-burn-image.yml`：检查成功 system run、取 source lock/base lock/kernel artifact/参考 ZIP，从参考包提取 2021 版 Wi-Fi 固件，装配 Burning Tool IMG 并独立检查。
- `.github/workflows/deliver-zipped-image.yml`：按 USB run 与 artifact ID 下载已验证 IMG，核对 provenance/hash，创建单 ZIP，检查 CRC、唯一 IMG、内部 SHA256SUMS、外部 `.sha256`，并对上传后的 ZIP artifact 做回读。
- `.github/workflows/build-and-deliver.yml`：唯一常规入口，在触发分支上串行调度前述 stages 并等待精确关联的 child run，最后把 ZIP 发布为唯一的最新 Release。失败/取消时只清理本入口创建的活动子 run；状态报告上传不含凭证。
- `scripts/orchestrate_build.py`、`scripts/cleanup_pipeline.py`、`scripts/export_pipeline_state.py`：唯一 run 关联、总超时、精确取消和简化 provenance 导出。
- `scripts/validate_reference_zip.py`：安全校验并在 Actions 临时目录解 ZIP。
- `scripts/prepare_kernel_input.py`、`scripts/create_kernel_manifest.py`：提取、核验 kernel build archive 和 provenance。
- `scripts/kernel_patch.py` + `config/kernel-patches.json`：补丁预检、应用和编译后确认。
- `scripts/audit_reference_wifi.py` + `config/wifi-firmware.lock.json`：从参考包提取并核对 Wi-Fi 固件。
- `scripts/publish_release.py`：下载并复核最终 ZIP，发布 Release，删除旧的交付 Release。
- `scripts/assemble_usb_image.py`：组合 FAT/ext4 和保留厂商组件；仅由 GitHub Ubuntu runner 用 `sudo python3` 执行。
- `scripts/verify_usb_image.py`：再次解开最终 Amlogic IMG，完整回读 sparse、boot 文件、rootfs、kernel/DTB/modules、CRC 和文件系统。

## 4. GitHub Actions 操作顺序

仓库默认分支 `main`。常规构建只用 Actions 页面唯一入口 **Build and deliver W103D USB image**，两个恢复字段留空后按 **Run workflow**。入口在触发它的分支（通常是 `main`）上按顺序创建参考预检、内核编译、系统重建、USB 组装/独立验证、ZIP 交付五个独立子 runs，不再为每次运行推送 tag。每个子 run 按 workflow path、head SHA、分支和唯一 correlation token 关联；每次 dispatch 前检查分支仍指向入口启动时的 commit，若构建期间有新提交推送到该分支则立即失败，需要重新运行。编排器只向下一阶段传递已确认成功的 run ID，拒绝“latest run”猜测，任何失败立即停止。最后入口把 ZIP 和 `.sha256` 发布为 GitHub Release，并删除旧的交付 Release。用户只 dispatch 一次，但 Actions 中会看到入口和多个 child runs。

取消入口时，cleanup 根据 dispatch 前写入的 stage、workflow、分支、token、commit 和参数查找精确 child，即使 GitHub 已接受 dispatch 但 run ID 尚未保存也能定位。仅取消此入口的活动子 run；取消 API 错误或 child 未终止会报告失败。

唯一入口有两个数字型恢复参数，互斥：

- `reuse_system_run_id`：从已成功系统 run 继续做版本命名 USB IMG、独立回读和 ZIP 交付。该 system artifact 必须未过期、包含确切 kernel archive 和完整来源锁。
- `reuse_usb_run_id`：从成功 USB IMG run 只做 ZIP。run 必须有通过验证的新格式 IMG basename、SHA 和全部来源报告；旧命名 run 会拒绝。

二者均空表示默认完整源码链；不能输入 artifact ID、branch 名或 shell 字符。用户需要排障时可以在 Actions 中单独运行各 child workflow，但它们不是常规入口。所有镜像操作都留在 GitHub，不通过本地 runner。

### 4.1 参考输入预检

启动 `Reference image preflight`，没有输入参数。工作流从固定 Release 拉 ZIP；解压和镜像分析均在 `ubuntu-24.04` runner。成功 artifact `reference-preflight-<run_id>` 应包括 ZIP/IMG validation、container check、reference layout 和 boot-chain summary。分析范围会保存 component hashes、`image.cfg`、boot FAT 文件摘要、sparse/ext4 元数据与可识别的 DTB/启动内容，不向 artifact 上传完整参考固件。

当前审查通过的输入预检 run 是 [36664112046](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36664112046)。不能只因 preflight 通过就跳过最终镜像回读。

### 4.2 编译 Ophub kernel

启动 `Compile pinned Ophub W103D kernel`，输入：

- `run_kernel_build`: `true`

resolver job 在 GitHub 上解析当前支持 W103D 的 Ophub/kernel 源并上传来源锁；compile job 是 ARM64 runner，检出锁定 commit、校验内核配置和 GNU toolchain SHA、调用固定版本的 Ophub kernel helper 并保存原始 tar archive、deb、sources lock 和 manifest。合成提交/标签的名字不是来源证据，检查 workflow run 的源 commit、compile step 与 artifact manifest。

成功 run artifact 名称为 `w103d-kernel-build-<run_id>`。

resolver job 锁定源码后，先只取锁定 kernel commit 中补丁涉及的文件（sparse checkout，约 20 秒），对 `config/kernel-patches.json` 列出的每个补丁执行 `git apply --check`，并把补丁路径和 SHA-256 写入 sources lock。补丁打不上、补丁文件被改动，或 Ophub 把 W103D 换到了补丁不针对的内核系列，都会在这里失败，不会进入长时间编译。compile job 校验 lock 中的补丁哈希后正式 `git apply`，编译完成后再用反向 `--check` 确认编译的是打过补丁的源码。不使用 Ophub `recompile -p`，因为它打补丁失败时只打印警告。

### 4.3 重建 Trixie W103D 系统

启动 `Rebuild W103D Trixie system with pinned Ophub kernel`，输入：

- `kernel_run_id`: 成功的 `compile-kernel` run ID

工作流会严格识别来源 workflow path/name，下载其中唯一的 sources lock、kernel manifest 和 `${version}.tar.gz`，用 archive SHA 校验后发布到 `kernel_stable` Release。发布时先检查并串行化同版本资产，再用 GitHub API 查询 Ophub 官方 Trixie arm64 server Releases，锁定最新通用 `-trunk` `.img.gz` 并按 API SHA-256 校验下载。之后以锁定 Ophub Action 和 board `s905l3a-w103d` 重建系统，关闭自动替换 kernel；验证器检查唯一符合 W103D/Trixie/kernel release 的 `.img.gz`、`/etc/os-release`、`/etc/ophub-release`、完整 kernel/DTB/modules 内容，并要求根内 `/etc/armbian-release VERSION` 与 base lock 的实际版本一致。Ophub release 自带 kernel track 只描述通用 base；最终 W103D kernel 版本以独立编译的 manifest 为准。

成功 system artifact 名为 `w103d-rebuilt-system-<run_id>`，包含压缩磁盘镜像、`system-image-verification.json`、`base-image.lock.json`、`sources.lock.json`、kernel manifest 和 Ophub commit。另有 diagnostics 和 candidate artifacts；失败时 candidate 只是未验收输入，不能交付。

历史错误 base 系统 run [36683231196](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36683231196) 不应用于最终 USB/ZIP。Ophub 新 base 系统 run [36692133844](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36692133844) 已成功：它复用了当时的已编译内核 artifact，从官方 26.11.0 generic Trixie release 重建 W103D，根内版本为 `26.11.0`，kernel release 是 `6.18.54-ophub`。该 system artifact 才是新版 USB 的输入。

### 4.4 生成 USB Burning Tool 单文件 IMG

启动 `Assemble and validate W103D USB Burning Tool image`，输入：

- `system_run_id`: 上一步成功系统重建的 run ID

此工作流拒绝非成功的系统 run，也校验 path/name、实际根内 `VERSION`、system report、base/source locks 与 kernel manifest 一致。它重新下载参考 ZIP、system artifact 中该次实际消费的不可变 kernel archive 及对应 Ophub boot-script source；在 runner 使用 `mkfs.fat`/mtools、e2fsprogs、`unmkinitramfs`、`mkimage` 和锁定 Amlogic packer。IMG basename 固定为 `W103D_Armbian_{armbian_version}_{kernel_version}_USB_Burning_Tool.img`，例如 `W103D_Armbian_26.11.0_6.18.54_USB_Burning_Tool.img`。值来自根内 `/etc/armbian-release VERSION` 和已验证 kernel manifest，不从 base 文件名推断。

常规用户只需运行唯一入口：

```sh
gh workflow run "Build and deliver W103D USB image" \
  --repo Remix123/w103d-armbian-usb-burn --ref main
```

默认两个恢复输入都为空，入口在触发分支上串行执行预检、内核、系统、USB、ZIP 五阶段并等待每个子 run 成功，最后发布 Release。手动单阶段命令只用于诊断，不能混用不同 run 的锁或 artifacts。

完整手动调度示例（run ID 要换成对应成功 run 的 ID）：

```sh
gh workflow run "Reference image preflight" --repo Remix123/w103d-armbian-usb-burn
gh workflow run "Compile pinned Ophub W103D kernel" \
  --repo Remix123/w103d-armbian-usb-burn -f run_kernel_build=true
gh workflow run "Rebuild W103D Trixie system with pinned Ophub kernel" \
  --repo Remix123/w103d-armbian-usb-burn -f kernel_run_id=<compile run ID>
gh workflow run "Assemble and validate W103D USB Burning Tool image" \
  --repo Remix123/w103d-armbian-usb-burn -f system_run_id=<system run ID>
```


核心步骤为：

1. 对 source system `.img.gz` 和 verification report 的 SHA-256 作逐字节绑定，再解析 MBR/文件系统并挂载读取；所有源系统修改/拷贝都在 GitHub runner。
2. 验证 source root release 内核文件和 kernel archive 一致；使用精确 W103D Linux DTB、同版 initramfs 和 modules。检查 kernel config 的 `CONFIG_MMC_MESON_GX` 与 `CONFIG_EXT4_FS`；若配置值为 `m`，最终 rootfs 和 initramfs 都必须带对应模块。
3. 按参考镜像预检所得大小/标签构建全新的 FAT `W103D_BOOT` 与 ext4 `W103D_ROOT`，复制完整新系统 `/boot` 内容后更新 eMMC 环境、主线 eMMC boot script、U-Boot legacy scripts、Linux DTB 路径和 root label。
4. 改 rootfs `/etc/fstab`、Ophub `DISK_TYPE=emmc`、禁用冲突 resize link、安装 W103D 专用首次扩容 service；清除 machine-id 和 SSH host keys，首启再生成。
5. 保留参考容器的厂商 payload，逐项 SHA-256 核对。使用 packer 重新组装最终 Amlogic image；pack 之后独立 verifier 再次解包，核对 image.cfg、每个 vendor component、整个 sparse 文件系统回读、FAT 文件内容、ext4 回读和 boot script CRC/body。执行真实打包之前，Ubuntu runner 还会运行 `mkimage` 合成脚本测试，覆盖 1/2/3/4-byte script body 长度和尾随字节拒绝。
6. 比对所有 archive 内 W103D kernel/DTB/modules 与最终 FAT/rootfs；对完整 initramfs 使用 `unmkinitramfs` 提取所有 cpio 部分，再按压缩格式规范化模块字节后与最终 `/lib/modules/<release>` 比较。
7. 只在上述验证全部通过时上传一个 raw `.img` 中间 artifact。上传后按 artifact ID 查询真实 metadata，以 IMG basename 校验；从 GitHub artifact download 接口读取，再判断响应是 raw bytes 还是 ZIP 包并提取唯一 IMG，核对下载副本 SHA-256 和 size 与原始 IMG 一致。随后 ZIP workflow 才生成最终用户下载件。

历史 USB run [36686786094](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094) 在错误的 26.8.1 base 上通过旧命名结构测试；artifact `11084660823` 未编码两个版本字段，不能当作新目标交付。新版 USB/ZIP 还未构建。

### 4.5 最终下载位置和 SHA 核对

ZIP delivery run 会按 USB artifact ID 下载 raw IMG，并对来源 run、校验报告、实际 basename、size/SHA 与完整 provenance 逐项核验。它创建 `W103D_Armbian_{armbian_version}_{kernel_version}_USB_Burning_Tool.zip`，包内只有一个对应 `.img`、根目录 `SHA256SUMS`、provenance 和验证报告；检查每个 ZIP member CRC、内部 SHA256SUMS 和唯一 IMG，然后上传 raw ZIP artifact 及独立 `<zip>.sha256` sidecar。

入口随后用 `scripts/publish_release.py` 发布：按 artifact ID 重新下载 ZIP 和 sidecar，再次核对 ZIP SHA-256、内部 `SHA256SUMS` 和 IMG SHA-256；创建 tag 为 `w103d-usb-<系统版本>-<内核版本>-<短 commit>` 的 Release（标记为 latest），上传 ZIP 和 `.sha256` 并核对资产大小和 digest；确认新 Release 无误后，才删除其他 `w103d-usb-*` Release 及其 tag。`kernel_stable`（内核 feed）和 `reference-input-*`（参考包）Release 不会被删除。Release 说明列出系统/内核版本、各源码 commit、补丁和固件哈希。仓库 [Releases](https://github.com/Remix123/w103d-armbian-usb-burn/releases/latest) 页面即为下载入口，不需要登录。

最终用户交付是一个版本化 ZIP，不是 GitHub artifact API 的传输 wrapper。下载后先校验外部 sidecar，再检查 ZIP CRC 和包内 `SHA256SUMS`；其根目录包含且只包含一个版本命名 IMG、manifest 和报告。不要手工拼接分卷，也不要将普通 Ophub `.img.gz` 或 system artifact 当成可直接烧录产物。

## 5. 系统与厂商分区的具体修改

### 5.1 boot FAT 文件

boot FAT 从 Ophub 重建镜像的完整 `/boot` 文件集合开始，随后覆盖明确的 kernel alias 与 W103D path，并保留必要的配置、uInitrd、initramfs、DTB 及厂商脚本。最终验证器校验 report 中所有 regular files 的 FAT readback 哈希，要求 `u-boot.ext`/`bootup.bmp` 等保留项与锁定参考值一致。

| 项目 | 处理 | 验证重点 |
|---|---|---|
| `zImage`、`Image` | 取锁定 kernel archive 的 `vmlinuz-<release>` 内容 | 两个文件均与 archive 字节相同 |
| Linux DTB | 放入 `/dtb-w103d/meson-g12a-w103d.dtb`，并保留 `/dtb/amlogic` 别名路径 | 与 Ophub DTB archive SHA 一致 |
| `uInitrd` | 选系统根文件和boot源中的同版 initramfs；缺 legacy header 时用 `mkimage -T ramdisk` 封装 | legacy header CRC、payload CRC、长度均正确；payload 解压检查全部 cpio 段 |
| `boot-emmc.cmd`/`boot.cmd` | 精确复制 locked Ophub mainline eMMC 脚本 | 来源 SHA 对应 source lock；`ramdisk_addr_r` 不被改写 |
| `boot.scr` | `mkimage -T script` 从相同 `boot.cmd` 生成 | legacy script table 长度/结束标识、单脚本精确 body（不补齐）、header CRC、data CRC 与源码一致 |
| `emmc_autoscript.cmd`/`emmc_autoscript` | 厂商 fallback 改根标签和 initrd path/变量，然后重建 legacy script | 存在显式 `setenv initrd_addr`；二进制脚本的 command 与 `.cmd` 一致 |
| `uEnv.txt` | 指定 kernel、uInitrd、FDT、ext4 根标签、rootwait 和控制台参数 | root selector 与 ext4 volume label 一致 |

普通 raw initramfs 不能被传给 `booti` 的单一 ramdisk 地址参数；U-Boot raw initrd 通常需要 address:size 的形式。这里固定成通过 64 字节 U-Boot legacy ramdisk header 的 `uInitrd`，同时在验证报告检查 header 与 payload CRC。

### 5.2 ext4 rootfs 文件

新 rootfs 保留 Ophub Debian Trixie 用户空间，并从官方通用 Trixie base 经过 Ophub W103D rebuild Action 生成；不是把旧 W103D Server 成品换 kernel 后冒充最新源系统。root filesystem payload 由重建系统复制到大小固定的 4 GiB ext4 镜像，再依照以下规则修改：

- 设置 ext4 标签 `W103D_ROOT` 与 reference layout 所用 UUID；`fstab` 用 labels 挂根与 boot，避免依赖 source image 原盘 UUID/设备枚举。
- `/etc/ophub-release` 设 `DISK_TYPE='emmc'`，让系统按设备内置存储描述运行。
- 移除通用 Armbian resize service 的 unit/link，加入受 label guard 保护的 `w103d-resize-rootfs.service` 和可执行 helper；首次启动只运行 `resize2fs`，不更改设备分区表。service link、脚本模式和内容均由 verifier 回读。
- `/etc/machine-id` 留空，D-Bus machine-id 连接到 `/etc/machine-id`；删除预置 SSH host keys，并让 sshd 首启生成新的设备私钥。不要把一个模板镜像的身份复制到每台盒子。
- rootfs 中完整 `/lib/modules/<release>` 或 `/usr/lib/modules/<release>` 模块树逐个与锁定 archive 的所有 regular `.ko` 内容比较；不能只核对 vermagic 字符串、目录名或一两个抽样模块。
- Wi-Fi 固件：Ophub base 自带的是 2020 版 N9（`d8b8488f…`，Build Time 20200604161656）。USB 阶段从锁定参考包的 rootfs 中提取 2021 版 `mt7663_n9_v3.bin`（`223f73f1…`，7663mp1827 / 20210308205639，即 W103D 出厂 Android `WIFI_RAM_CODE_MT7663.bin`，与 pigeon2049 `board/w103d/firmware/manifest.json` 相同）和 `mt7663pr2h.bin`（`534f2152…`），按 `config/wifi-firmware.lock.json` 校验后写入 `/usr/lib/firmware/mediatek/`，替换原文件并删除可能遮蔽它的压缩副本。verifier 从最终镜像回读这两个文件的哈希。这两个文件不属于任何 deb 包，apt 升级不会覆盖。

禁止根据 rootfs 文件大小推算设备分区起点。本工作流未在实体设备上分区，也不把 Linux 磁盘 IMG 的 MBR 直接刷入 eMMC。

### 5.3 Raw、Android sparse 与 Amlogic packer

`bootfs.raw` 与 `rootfs.raw` 是可被 `fsck.vfat`/`e2fsck` 检查的普通文件系统文件。Amlogic payload 使用 Android sparse 格式表示 RAW/FILL/DONT_CARE/CRC chunks。sparse header 不含独立 header CRC；解码器检查 sparse header 字段、header 中可选的 whole-image checksum、显式 CRC chunk、所有 chunk 长度、逻辑尺寸和 EOF 无多余尾随 bytes，并比较还原文件 SHA-256。零块必须用正确长度 FILL chunk 编码，不能在 chunk 头之后多写字节。

最终 container 使用原参考包完整 `image.cfg` 和 vendor binaries。完成 pack 后跑 packer `-c`，独立 verifier 再 `-d` 重新展开最终 IMG；两个 sparse partition payload 都完整还原并回读，不以 packer exit code 或最终体积作为 sole proof。

## 6. 离线验证、交付与设备验收

### 6.1 GitHub 成品验收门槛

只有在 USB verifier 与 ZIP workflow 全绿、ZIP 按 artifact ID 重新下载并完整回读通过，才把该包当成最终交付：

- 单个 `W103D_Armbian_<实际系统版本>_<内核版本>_USB_Burning_Tool.zip`；
- ZIP 根目录唯一 raw IMG `W103D_Armbian_<实际系统版本>_<内核版本>_USB_Burning_Tool.img` 与能直接用于 `sha256sum -c` 的 `SHA256SUMS`；
- 外部 `<zip basename>.sha256` sidecar，以及 `zip-delivery-validation.txt`、ZIP CRC/内 SHA 读回报告；
- `verification-report.json`、`assembly-input-report.json`、`artifact-download-validation.txt`；
- `reference.lock.json`、`sources.lock.json`、`kernel-input-manifest.json`、`base-image.lock.json`、`system-image-verification.json`、`wifi-firmware-manifest.json`、kernel source run provenance；
- Actions run URL、source 分支和 commit、IMG/ZIP artifact ID、Release tag、size、SHA-256 和成功结论。

验证必须涵盖：Amlogic 容器 decode/check；vendor payload hash；`image.cfg` 与容器项目检查；sparse header fields/chunk/image CRC/trailing EOF；FAT `fsck.vfat -n` 和全部 boot 文件回读；ext4 `e2fsck -fn`/label/read-only mount；Trixie `os-release`、Ophub `DISK_TYPE=emmc`、fstab、machine-id、D-Bus link、SSH key 清理、resize service；主线 boot-emmc 与 vendor fallback 的 U-Boot legacy CRC及源码映射；kernel config、kernel/DTB 所有字节、rootfs全部模块、initramfs全部模块；最终 artifact 重新下载后 bytes+SHA256。这里的 container/image.cfg 检查不等于验证设备 MBR 或 eMMC 起始偏移。

本地 `py_compile`/pyflakes/YAML 检查只检验脚本语法和静态问题；synthetic sparse 或 U-Boot header 测试不等于真实固件运行；GitHub `mkimage`、packer、mount/fsck 回读才是本次离线镜像验证。所有层次都不能代替刷机。

### 6.2 Windows USB Burning Tool 操作（只用于最终烧录）

Windows 只承担最后的用户侧导入和烧录，不参与编译、镜像挂载、解包、系统修改、重打包或验证。建议顺序：

1. 从仓库最新 Release 下载 ZIP 和同名 `.sha256`；不要下载普通 Armbian `.img.gz` 或 Actions 里的 USB raw artifact 作为最终件。
2. 用 PowerShell 校验 ZIP 外部 hash、解压后对 IMG 和 provenance 运行包内 SHA256SUMS：

   ```powershell
   $zip = ".\W103D_Armbian_26.11.0_6.18.54_USB_Burning_Tool.zip"
   $sidecar = Get-Content "${zip}.sha256"
   $expected = ($sidecar -split '\s+')[0]
   $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
   if ($actual -ne $expected) { throw "ZIP SHA-256 mismatch" }
   Expand-Archive -LiteralPath $zip -DestinationPath .\w103d-package
   Get-Content .\w103d-package\SHA256SUMS | ForEach-Object {
       $expected = $_.Substring(0, 64)
       $file = Join-Path .\w103d-package ($_.Substring(66))
       $actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
       if ($actual -ne $expected) { throw "SHA-256 mismatch: $file" }
   }
   Get-FileHash .\w103d-package\W103D_Armbian_26.11.0_6.18.54_USB_Burning_Tool.img -Algorithm SHA256
   ```

3. 保留原始可恢复参考包及其 SHA256；确认 USB 驱动、设备进入下载模式方式、供电和线缆。准备串口与已验证恢复手段。
4. 在与该盒子相匹配的 Amlogic USB Burning Tool 中导入从 ZIP 解出的版本命名 IMG，记录工具版本、导入结果和 log。不要导入 `.img.gz`、普通 Ophub 磁盘镜像、ZIP 本身或 artifact reports。
5. 烧录选项沿用该 W103D 的已验证操作流程。本项目没有验证擦除模式、密钥擦除或全盘擦除的通用设置，不能猜测一个适用于所有盒子的选项。
6. 成功提示只表示烧录工具流程到达完成。通过 UART 记录 DDR/U-Boot、厂商脚本、二级 U-Boot、Linux、root mount 和首次扩容；完成冷启动、重启及断电启动测试后，再检查网络、存储、无线、温度和外设。

设备启动后可收集：

```sh
uname -a
cat /etc/os-release
cat /etc/ophub-release
cat /proc/cmdline
findmnt /
findmnt /boot
lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS
df -hT / /boot
systemctl --failed
systemctl cat w103d-resize-rootfs.service
journalctl -b -u w103d-resize-rootfs.service --no-pager
journalctl -b -p warning --no-pager
dmesg | grep -Ei 'mmc|ext4|error|fail|ethernet|wifi|firmware'
ls -ld /lib/modules/"$(uname -r)"
ls -l /etc/ssh/ssh_host_*_key.pub
cat /etc/machine-id
```

检查扩容服务只扩大已有 root filesystem，并与 label guard 一致；不要未经分析再运行通用 `armbian-install`、重写分区表或执行整盘命令。失败时保留串口、工具日志和设备状态，优先用已验证适配本机的恢复包恢复。当前项目不能承诺设备现有 eMMC 分区、校准数据或健康状态。

## 7. 日常操作和故障处理

### 7.1 重新构建同一版本

如需重复打包完全相同的 source/kernel/system，请先复用已有成功 compile run 和 system run 的锁定 artifacts；它们明确绑定 commit 和 SHA。重新运行 resolver 会重新选择届时的“最新”版本，不能把新 run 称为同一次版本复现。需要更新最新上游时才重新 dispatch 编译，然后：

1. 核对新 kernel run 的 sources lock、编译报告和 archive SHA。
2. 将这个成功 kernel run ID 传给 `Rebuild W103D Trixie system with pinned Ophub kernel`。
3. 将成功 system run ID 传给 `Assemble and validate W103D USB Burning Tool image`。
4. 保存每个 run ID、source lock、artifact SHA 和最终报告。不同 run 之间不要混合 kernel archive、source lock 和 system image。

若只是重跑最终 pack，允许引用已有成功的 system run；工作流自身会锁定该 run 的准确 artifacts，但应检查留存期限和 Release archive 是否仍在。

### 7.2 更换 Ophub 当前 kernel 系列/版本

不要简单把 `6.18.54` 全局替换成另一个号码。重新运行 resolver 后检查 W103D database row、kernel series、commit SHA、config path/hash、版本 Makefile 和 toolchain asset/hash。编译后确认完整的 deb 与 Ophub nested tar archive；核实 kernel release 及 `vmlinuz/config/System.map/DTB/modules` 的路径实际格式，再启动系统 rebuild。

新 Trixie 系统必须把这个 kernel archive 内容逐字节接入，并由 system report 锁定；重新解压最终 initramfs 检查所需 MMC/ext4 modules、压缩方式、root mount 和 DTB路径。先运行 system verifier，再运行 USB pack/verifier。不同 kernel 造成最终 IMG SHA 变化正常；检查报告中的逐项 hash 与源锁，不追求复用旧成品 hash。硬件冷启动/网络/在线 `armbian-update` 均需重新验收。

### 7.3 错误处理准则

| 错误 | 必查信息 | 处置边界 |
|---|---|---|
| Reference ZIP hash/member 不符 | Release asset、reference lock、ZIP 中成员列表 | 停止；不得改锁绕过校验 |
| resolver 找不到唯一 W103D row | 当前 pinned model database 表头/列索引和 enable 状态 | 修解析逻辑；不得强行指定板型绕过支持检查 |
| 补丁预检失败 | resolver 的 `Pre-flight` step 日志、锁定 kernel commit、`config/kernel-patches.json` | 上游改了补丁涉及的文件或换了内核系列：在新源码上重新移植并上板测试补丁，更新补丁和 SHA-256；不得跳过补丁继续出镜像 |
| kernel compile失败 | compile step、source SHAs、config/toolchain 哈希 | 修复后产生新 run；不把中间 artifacts 当成功 |
| Wi-Fi 固件提取/哈希失败 | USB run 的固件提取 step、`reference-wifi-audit.json`、`config/wifi-firmware.lock.json` | 停止；不得改用 base 自带固件或改锁绕过 |
| Release 发布失败 | 入口 run 的 `Publish` step 日志 | 旧 Release 只在新 Release 校验通过后删除，失败时旧 Release 仍在；修复后用 `reuse_usb_run_id` 重跑入口即可，不必重编 |
| 分支在构建期间被推送新提交 | 入口 summary 中的 `moved from ... to ...` | 等推送完成后重新运行入口 |
| system 找错 IMG 或文件系统识别失败 | 候选名、`sfdisk --json`、udev、`blkid`、partition discovery报告 | 只对识别路径做修正；不可关闭 MBR/filesystem 检查 |
| initramfs/模块不一致 | nested tar路径、kernel release、所有 module hash | 停止，修正来源接入或版本匹配；不可抽样后称完整验证 |
| packer 或 sparse 失败 | packer stderr、chunk boundary/CRC/EOF、disk space | 保存 candidate 和 diagnostics；candidate不是成品 |
| USB artifact 名称/格式/SHA不符 | artifact ID metadata、raw/ZIP签名、下载尺寸和哈希 | 不交付；查明平台传输格式后重新验证单 IMG |

base 镜像下载有连接/总时限和速度限制；失败可由系统工作流重新运行，checksum 必须仍与官方 sidecar 一致。不要中途本地解压镜像“辅助排障”，应从 Actions 日志和 diagnostics 找证据。

## 8. 来源、工具和代码入口

- [Ophub W103D 上游仓库](https://github.com/ophub/amlogic-s9xxx-armbian)：当前设备 database/build scripts/action/kernel selection。
- [Pigeon W103D burn branch](https://github.com/pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18)：打包流程参考；不是系统/内核源码来源。
- [Khadas utils](https://github.com/khadas/utils/tree/a3604ed6d6863d1946d26d0ac8e765aef6f17430/aml-flash-tool/tools)：已固定 commit 和 Linux packer 哈希，见 [toolchain.lock.json](../config/toolchain.lock.json)。
- [Android sparse image 格式](https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/sparse_format.h)：chunk 解码需验证 header、CRC、逻辑尺寸和完整 EOF。
- [U-Boot mkimage source](https://github.com/u-boot/u-boot/blob/master/tools/mkimage.c)：legacy header、CRC 和 script multi-file table 的实现依据；具体 pack 命令由 Linux workflow 执行。
- 可追溯 workflow：[reference-preflight.yml](../.github/workflows/reference-preflight.yml)、[compile-kernel.yml](../.github/workflows/compile-kernel.yml)、[build-system.yml](../.github/workflows/build-system.yml)、[build-usb-burn-image.yml](../.github/workflows/build-usb-burn-image.yml)。

没有移植旧手册 macOS `build_w103d_macos.py`、Windows/Cygwin `build_w103d_windows.py`、Cygwin e2fsprogs/mtools toolchain 或历史恢复脚本。Windows 只保留第 6.2 节 USB Burning Tool 的使用说明。原旧手册和相关旧脚本在修改前已有只读副本保存在本工作目录的 `work/original-guide-snapshot/`，用于回溯旧启动链、rootfs 和设备验收注意事项；snapshot 不是本次新的构建证据。
