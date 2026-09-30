# W103D Armbian USB Burning Tool 镜像构建与验收手册

**适用对象：ZTE W103D / Amlogic S905L3A。** 固件解包、内核编译、Trixie 系统重建、USB Burning Tool 镜像封装、ZIP 压缩和回读验证均在 GitHub Actions/Linux runner 完成。常规用户只 dispatch 一个入口；编排器顺序创建多个独立子 workflow runs。最终下载件是单 ZIP，里面恰有一个可直接导入 USB Burning Tool 的 raw IMG、可用 `sha256sum -c` 验证的 `SHA256SUMS` 以及 provenance/验证报告。ZIP 本身不能导入烧录工具。

本流程以 [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian) 当前支持 W103D 的源代码和内核为系统来源。参考分支 [pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18](https://github.com/pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18) 仅用于理解板级 USB 烧录打包方法；不得从该分支替换系统或内核来源。

> 结果边界：GitHub 工作流只能给出来源可追溯的离线镜像和结构验证。没有实际用 USB Burning Tool 导入、没有刷写 W103D、没有串口冷启动证据时，不能宣称硬件适配成功。

## 1. 当前执行状态

仓库：[Remix123/w103d-armbian-usb-burn](https://github.com/Remix123/w103d-armbian-usb-burn)。截至 2026-09-30，已知运行与其适用范围如下：

| 阶段 | GitHub run | 结果 |
|---|---|---|
| 参考包云端预检 | [36664112046](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36664112046) | 成功。ZIP/IMG 哈希、容器和参考布局解析通过 |
| 内核构建 | [36669269184](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36669269184) | 内核已编译成功；最初仅因验证 step 缺 `rg` 退出失败 |
| 已编译内核恢复校验 | [36680464101](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36680464101) | 成功；复核既有 artifact，没有重新编译 |
| 旧基础镜像系统重建 | [36683231196](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36683231196) | 当时成功，但使用错误选取的 Armbian archive Odroid N2 26.8.1 base；作为历史诊断，不符合“最新 Ophub 26.11.0 系统”目标 |
| 旧版 USB 结构验证 | [36686786094](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094) | 历史 packer/boot/rootfs 离线验证通过；输出名不含系统版本，不能当新目标最终交付 |
| 旧 base 版本命名测试 | [36690877359](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36690877359) | 使用 26.8.1 系统的打包测试成功；不是最新系统目标，不能作为新版本 ZIP 来源 |
| Ophub 最新系统轻量重建 | [36692133844](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36692133844) | 成功；锁定 Ophub official 26.11.0 generic Trixie base，根内 `/etc/armbian-release VERSION`、kernel、DTB、全部 modules 检查通过；复用既有 recovery kernel，不重编 |

一键入口测试 [36691126247](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36691126247) 已在预检后被取消：当时发现 base 来源选错。它没有完成内核、系统、USB 或 ZIP 阶段。26.11.0 system artifact 已成功；对应的新版名称 USB/ZIP run 尚未启动，故当前没有符合最新系统和命名要求的最终交付。历史 IMG 不能改名后冒充新版。所有硬件烧录、冷启动和在线升级仍未执行。

## 2. 镜像类型、布局与两级启动链

### 2.1 两种 `.img` 不能混用

| 文件类型 | 内容 | 用途 |
|---|---|---|
| Ophub `Armbian_...img.gz` / `.img` | MBR 磁盘镜像，含 Linux 启动分区和根文件系统 | 系统重建的基础输入；可写入普通存储介质 |
| `W103D_Armbian_26.8.1_Server.img` | Amlogic Burning Tool 容器，含 USB 下载组件、项目表和各分区 payload | 本机兼容的板级引导参考；不能直接拿普通 Linux IMG 替代 |
| 最终 `...USB_Burning_Tool.img` | 从参考容器复制板级部分，再加入本次重建的 FAT/ext4 payload 的 Amlogic 容器 | Windows USB Burning Tool 的单文件导入输入 |

只改扩展名不会转换容器。`aml_image_v2_packer -c` 通过也只证明容器格式检查通过，不代表盒子已启动。

### 2.2 参考容器布局

参考 IMG 在 GitHub `Reference image preflight` 内解包和分析。本仓库的 [reference-layout-lock.json](../config/reference-layout-lock.json) 固定它的 `image.cfg` 以及经逐字节核对的厂商 payload 哈希。`[LIST_VERIFY]` 含 `system.PARTITION` 与 `data.PARTITION` 两个 Android sparse 项；保留项包括 `DDR.USB`、`_aml_dtb.PARTITION`、`boot.PARTITION`、`recovery.PARTITION`、`dtbo.PARTITION`、`vbmeta.PARTITION`、`logo.PARTITION`、`platform.conf`。最终重新读取容器，保留项必须与本次参考包 SHA-256 一致。

参考 boot FAT sparse 解码为 1 GiB FAT，卷标 `W103D_BOOT`。data sparse 解码为 4 GiB ext4，卷标 `W103D_ROOT`，UUID `01b94932-6a4a-4c81-9a71-20bd55b675a8`。这些是文件系统 payload 的测量值，不是 eMMC 起始扇区或物理分区偏移。不得把旧手册、文件大小或 raw payload 大小当成 partition start。Amlogic `image.cfg` 和厂商分区 bootstrap 都保持参考包内容；本流程没有读取设备 eMMC 分区表。

`_aml_dtb.PARTITION` 是早期厂商启动阶段使用的 DTB；boot FAT 中的 `dtb-w103d/meson-g12a-w103d.dtb` 是 Linux 主线内核 DTB。两者职责不同，不能互相替换。

### 2.3 启动过程和变量边界

1. 参考包的 DDR、USB U-Boot、厂商 DTB、boot/recovery、dtbo、vbmeta、logo 和 platform 配置维持原始字节。
2. 厂商引导链从 FAT 读取 `emmc_autoscript`。可读源码 `emmc_autoscript.cmd` 与 U-Boot legacy script 二进制必须匹配；打包器重新生成脚本并检查 legacy header CRC、数据 CRC、长度和 script table。
3. 脚本优先进入 W103D 的 `u-boot.ext` 主线链路，再按 eMMC 的 Ophub `boot-emmc.cmd` 读取 `uEnv.txt`，加载内核、legacy `uInitrd` 和 Linux DTB。
4. 主线 eMMC 脚本保留 U-Boot 标准变量 `ramdisk_addr_r`；从锁定 Ophub commit 复制，不能擅自改成另一名字。
5. 备用厂商 `emmc_autoscript.cmd` 路径依参考包更新逻辑做兼容性调整：`ramdisk-w103d.img` 指向 `uInitrd`，boot 参数使用其显式 `setenv initrd_addr` 地址变量。只改这一条厂商路径，不重写主线脚本变量。
6. `uEnv.txt` 指定 `LINUX=/zImage`、`INITRD=/uInitrd`、W103D DTB 和 `root=LABEL=W103D_ROOT`。原始 initramfs 如不是 U-Boot legacy ramdisk，打包时先创建并验证 64 字节 legacy header；因此 `booti` 单地址参数与实际 uInitrd 格式相配。
7. Linux 根据 root label 挂载 ext4；`/etc/fstab` 通过 `W103D_ROOT` 和 `W103D_BOOT` 挂载 root 与 `/boot`。首次启动专用服务扩展已有 root filesystem，不重写 GPT/MBR 或厂商分区布局。

对应的可审查实现：[assemble_usb_image.py](../scripts/assemble_usb_image.py)、[verify_usb_image.py](../scripts/verify_usb_image.py)、[inspect_reference.py](../scripts/inspect_reference.py)。

## 3. 输入文件、锁和仓库文件

### 3.1 固定参考输入

用户指定的原始参考包：`/Users/amino/Documents/CodexProj/410Driver/W103D/W103D_Armbian_26.8.1_Server.img`。

| 项目 | 值 |
|---|---|
| IMG 大小 | 1,919,320,976 字节 |
| IMG SHA-256 | `60b0c3355eda080cdf0c350e063bb6c8ac664f0dea872f5d5377d8a53503159d` |
| ZIP 内成员 | 仅 `W103D_Armbian_26.8.1_Server.img`，无父目录 |
| ZIP SHA-256 | `7db21d4bba6e13a2176eed81894433f2dadd8acf1c4a0cd6bac9f64797a47dc5` |
| Release tag / asset | `reference-input-2026-09-30` / `W103D_Armbian_26.8.1_Server.zip` |

来源锁在 [reference.lock.json](../config/reference.lock.json)。预检和 USB workflow 都会验证 ZIP 的 SHA-256、只含一个成员、成员路径安全、解压后 IMG 长度与 IMG SHA-256。此参考 ZIP 放 GitHub Release，不放 Git repository；ZIP 内不得附带 README、日志或其他固件。

准备或更新这个输入时，只在本地对指定 IMG 做哈希和 ZIP 封装，不运行 packer、不解包/挂载镜像、不构建系统：

```sh
cd /Users/amino/Documents/CodexProj/410Driver/W103D
zip -j W103D_Armbian_26.8.1_Server.zip W103D_Armbian_26.8.1_Server.img
unzip -Z1 W103D_Armbian_26.8.1_Server.zip
shasum -a 256 W103D_Armbian_26.8.1_Server.img W103D_Armbian_26.8.1_Server.zip
gh release view reference-input-2026-09-30 --repo Remix123/w103d-armbian-usb-burn
gh release upload reference-input-2026-09-30 W103D_Armbian_26.8.1_Server.zip \
  --repo Remix123/w103d-armbian-usb-burn
```

先确认 Release 资产尚不存在，检查 `unzip -Z1` 恰好只列出上述 IMG basename，并且两个 SHA-256 与 `config/reference.lock.json` 完全一致后才上传。ZIP 文件哈希还受归档元数据影响；即使 IMG 未变，重新压缩出的 ZIP 也可能与原 ZIP 字节不同。因此正常复现直接使用已上传且通过 lock 的 Release 资产；若 ZIP 哈希不一致，停止上传并保留原资产。确需更换输入时，先审查新的 IMG/ZIP hash、更新 lock 和布局报告，再以新版本化 Release/tag/asset 保存，避免覆盖已审查输入。

如果重传参考包，先确认用户指定文件没有改变；重新计算两层 SHA-256、更新 lock、再上传 Release。不要使用压缩包多一层目录、同名旧资产或来自其他型号的近似文件。

### 3.2 上游代码、内核和工具锁

`resolve_sources.py` 查询 Ophub 主仓 [model_database.conf](https://github.com/ophub/amlogic-s9xxx-armbian/blob/main/build-armbian/armbian-files/common-files/etc/model_database.conf) 中唯一 enabled 的 W103D 行，并按其 `stable/X.Y.y` 字段动态确定 kernel series；不会在 resolver 中写死 `6.18`。当前行标识 `s905l3a-w103d`、DTB `meson-g12a-w103d.dtb`、U-Boot `u-boot-w103d.bin`、series `stable/6.18.y`。然后将 Ophub 主仓、`ophub/linux-X.Y.y` Kernel tree、`ophub/kernel` 配置 repo 和 toolchain asset 的 commit/hash 锁入 `sources.lock.json`。后续 clone/checkout 使用这些不可变 commit，而不是 floating `main`。官方 [Ophub compile-kernel workflow](https://github.com/ophub/amlogic-s9xxx-armbian/blob/main/.github/workflows/compile-kernel.yml) 的 `kernel_source=ophub` 路径也是先解析 `ophub/linux` 分仓，再调用主仓 kernel build helper；kernel tree 与配置库分仓是 Ophub 官方依赖关系，不是把 Pigeon fork 作为替代源码。

官方 [Ophub kernel helper](https://github.com/ophub/amlogic-s9xxx-armbian/blob/main/compile-kernel/tools/script/armbian_compile_kernel.sh) 将内核配置定位到 `ophub/kernel` 的 `kernel-config/release`，GNU ARM toolchain 也从 `ophub/kernel` 的 `dev` release 获取。当前流程在锁中记录 config commit/path/hash 和 Ophub `arm-gnu-toolchain-15.3.rel1` asset SHA，固定工具链用于复现，不随着每次重跑浮动变更。系统 rebuild Action、W103D board profile 与 `boot-emmc.cmd` 来自锁定的 `ophub/amlogic-s9xxx-armbian` 主仓 commit。

`resolve_base_image.py` 从 Ophub 官方 Releases 的 Trixie arm64 server releases 中选通用 `-trunk` `.img.gz`，先按 Armbian 版本数值排序、再按 asset `updated_at` 选择；胜出候选必须有 API SHA-256，不会在缺 hash 时悄悄退回旧版本。`base-image.lock.json` 保存 repository/release/asset ID/tag/name/size/digest/hash/API 与 browser URL/time/system version，并随 system artifact 和 ZIP provenance 保存。当前最新候选来自 `Armbian_trixie_arm64_server_2026.09`：asset `574482696`，`Armbian_26.11.0-trunk_trixie_arm64_6.18.52.img.gz`，847455217 bytes，SHA-256 `b639a9e071e1523a3b920e4754414af3c720f12a19df221841c2372a4f7139b0`。system run [36692133844](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36692133844) 已成功并报告 `/etc/armbian-release VERSION=26.11.0`、kernel release `6.18.54-ophub`。base 所带 `6.18.52` 是通用镜像构建标签；我们交付的 W103D kernel 是独立由最新 enabled W103D series 编译并强制比较安装文件的 `6.18.54`。

当前已成功系统 run 对应的锁值是 build artifact 的权威记录。该次决策使用 Ophub `8b601ee74525a5cc68661d372c613666b9fafddd`，kernel tree `ophub/linux-6.18.y` commit `0f189d6b3197b94a8fbc96a670f0095cd63ce1a9`，kernel config metadata commit `4dbcaf0f83d63bdf4efd343a3af922b2866408f2`，版本 `6.18.54`、release `6.18.54-ophub`。下次构建需以新 run 的 sources lock 为准，不把此版本写死为“最新”。

Amlogic packer 在 [toolchain.lock.json](../config/toolchain.lock.json) 中固定：Khadas `utils` commit `a3604ed6d6863d1946d26d0ac8e765aef6f17430`，`aml_image_v2_packer` SHA-256 `8123b1295abb3262c76b650ba024975e38aedfd49b19f59a09f5920738ef1597`。它是独立的 Amlogic 容器格式打包工具，不是系统或内核源码；需 GitHub x86_64 Linux runner 的 IA32 执行支持。Pigeon W103D burn 分支仅作打包流程参考。USB job 的真实 packer `-c`、`-d`、`-r` 结果由 GitHub run 日志证实；本地 macOS 不运行该工具。

### 3.3 目录和脚本职责

- `.github/workflows/reference-preflight.yml`：参考 ZIP 云端解压、Amlogic packer 预检、镜像布局/DTB/boot 链分析。
- `.github/workflows/compile-kernel.yml`：源解析与 arm64 内核编译；不会在系统重建时重新取 floating kernel。
- `.github/workflows/recover-kernel-artifact.yml`：仅针对已编译成功、后续验证工具缺失的既有 run 做来源核验和 artifact 恢复，不会重编。
- `.github/workflows/build-system.yml`：采用官方通用 Trixie base，经锁定 Ophub source Action 针对 W103D 重建，强制消费 kernel archive 并比较实际镜像内容。
- `.github/workflows/build-usb-burn-image.yml`：检查成功 system run、取 source lock/base lock/kernel artifact/参考 ZIP，装配 Burning Tool IMG 并独立检查。
- `.github/workflows/deliver-zipped-image.yml`：按 USB run 与 artifact ID 下载已验证 IMG，核对 provenance/hash，创建单 ZIP，检查 CRC、唯一 IMG、内部 SHA256SUMS、外部 `.sha256`，并对上传后的 ZIP artifact 做回读。
- `.github/workflows/build-and-deliver.yml`：唯一常规入口，固定 source commit/tag 后串行调度前述 stages，并等待精确关联的 child run。失败/取消时只清理本入口创建的活动子 run；状态报告上传不含凭证。
- `scripts/orchestrate_build.py`、`scripts/cleanup_pipeline.py`、`scripts/export_pipeline_state.py`：唯一 run 关联、总超时、精确取消和简化 provenance 导出。
- `scripts/validate_reference_zip.py`：安全校验并在 Actions 临时目录解 ZIP。
- `scripts/prepare_kernel_input.py`、`scripts/create_kernel_manifest.py`、`scripts/recover_kernel_artifact.py`：提取、核验 kernel build archive 和 provenance。
- `scripts/assemble_usb_image.py`：组合 FAT/ext4 和保留厂商组件；仅由 GitHub Ubuntu runner 用 `sudo python3` 执行。
- `scripts/verify_usb_image.py`：再次解开最终 Amlogic IMG，完整回读 sparse、boot 文件、rootfs、kernel/DTB/modules、CRC 和文件系统。

## 4. GitHub Actions 操作顺序

仓库默认分支 `main`。常规构建只用 Actions 页面唯一入口 **Build and deliver W103D USB image**，两个恢复字段留空后按 **Run workflow**。入口会先固定代码 tag/commit，再按顺序创建参考预检、内核编译、系统重建、USB 组装/独立验证、ZIP 交付五个独立子 runs。每个子 run 按 workflow path、head SHA、tag 和唯一 correlation token 关联；编排器只向下一阶段传递已确认成功的 run ID，拒绝“latest run”猜测，任何失败立即停止。最终入口 summary 提供 ZIP、`.sha256` sidecar、内部 IMG 和校验 hash 的直链。用户只 dispatch 一次，但 Actions 中会看到入口和多个 child runs。

取消入口时，cleanup 根据 dispatch 前写入的 stage、workflow、tag、token、commit 和参数查找精确 child，即使 GitHub 已接受 dispatch 但 run ID 尚未保存也能定位。仅取消此入口的活动子 run；取消 API 错误或 child 未终止会报告失败，不删除 source tag，tag 留作 provenance。

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

成功 run artifact 名称为 `w103d-kernel-build-<run_id>`。若编译确实完成，但只有最后 verifier 报 `rg: command not found`，先读取 source run jobs/logs。仅当 compile step 和 artifact upload 均成功、失败 signature 精确匹配时，使用 `Recover and verify existing W103D kernel artifact`：

- `source_run_id`: 原始 compile run ID
- `expected_head_sha`: 原始 compile workflow 的精确 `head_sha`

Recovery 会验证原 run workflow path、commit、关键 step 状态、nested archive inner checksums 和 payload hashes，再上传独立 recovery artifact。任何编译失败、源不匹配或其他 verifier failure 都不允许用此 recovery workflow 掩盖。当前使用的原 kernel run `36669269184`、recovery run `36680464101`；不需要重复编译。

### 4.3 重建 Trixie W103D 系统

启动 `Rebuild W103D Trixie system with pinned Ophub kernel`，输入：

- `kernel_run_id`: 成功的 `compile-kernel` run ID，或已审查成功的 recovery run ID

工作流会严格识别来源 workflow path/name，下载其中唯一的 sources lock、kernel manifest 和 `${version}.tar.gz`，用 archive SHA 校验后发布到 `kernel_stable` Release。发布时先检查并串行化同版本资产，再用 GitHub API 查询 Ophub 官方 Trixie arm64 server Releases，锁定最新通用 `-trunk` `.img.gz` 并按 API SHA-256 校验下载。之后以锁定 Ophub Action 和 board `s905l3a-w103d` 重建系统，关闭自动替换 kernel；验证器检查唯一符合 W103D/Trixie/kernel release 的 `.img.gz`、`/etc/os-release`、`/etc/ophub-release`、完整 kernel/DTB/modules 内容，并要求根内 `/etc/armbian-release VERSION` 与 base lock 的实际版本一致。Ophub release 自带 kernel track 只描述通用 base；最终 W103D kernel 版本以独立编译的 manifest 为准。

成功 system artifact 名为 `w103d-rebuilt-system-<run_id>`，包含压缩磁盘镜像、`system-image-verification.json`、`base-image.lock.json`、`sources.lock.json`、kernel manifest 和 Ophub commit。另有 diagnostics 和 candidate artifacts；失败时 candidate 只是未验收输入，不能交付。

历史错误 base 系统 run [36683231196](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36683231196) 不应用于最终 USB/ZIP。Ophub 新 base 系统 run [36692133844](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36692133844) 已成功：它复用 kernel recovery run `36680464101`，从官方 26.11.0 generic Trixie release 重建 W103D，根内版本为 `26.11.0`，kernel release 是 `6.18.54-ophub`。该 system artifact 才是新版 USB 的输入。

### 4.4 生成 USB Burning Tool 单文件 IMG

启动 `Assemble and validate W103D USB Burning Tool image`，输入：

- `system_run_id`: 上一步成功系统重建的 run ID

此工作流拒绝非成功的系统 run，也校验 path/name、实际根内 `VERSION`、system report、base/source locks 与 kernel manifest 一致。它重新下载参考 ZIP、system artifact 中该次实际消费的不可变 kernel archive 及对应 Ophub boot-script source；在 runner 使用 `mkfs.fat`/mtools、e2fsprogs、`unmkinitramfs`、`mkimage` 和锁定 Amlogic packer。IMG basename 固定为 `W103D_Armbian_{armbian_version}_{kernel_version}_USB_Burning_Tool.img`，例如 `W103D_Armbian_26.11.0_6.18.54_USB_Burning_Tool.img`。值来自根内 `/etc/armbian-release VERSION` 和已验证 kernel manifest，不从 base 文件名推断。

常规用户只需运行唯一入口：

```sh
gh workflow run "Build and deliver W103D USB image" \
  --repo Remix123/w103d-armbian-usb-burn --ref main
```

默认两个恢复输入都为空，入口固定 source commit/tag，串行执行预检、内核、系统、USB、ZIP 五阶段并等待每个子 run 成功。手动单阶段命令只用于诊断，不能混用不同 run 的锁或 artifacts。

完整手动调度示例（run ID 要换成对应成功 run 的 ID）：

```sh
gh workflow run "Reference image preflight" --repo Remix123/w103d-armbian-usb-burn
gh workflow run "Compile pinned Ophub W103D kernel" \
  --repo Remix123/w103d-armbian-usb-burn -f run_kernel_build=true
gh workflow run "Rebuild W103D Trixie system with pinned Ophub kernel" \
  --repo Remix123/w103d-armbian-usb-burn -f kernel_run_id=36680464101
gh workflow run "Assemble and validate W103D USB Burning Tool image" \
  --repo Remix123/w103d-armbian-usb-burn -f system_run_id=36683231196
```

如果 kernel compile 成功但只因 verifier 缺命令失败，检查日志后才能运行 recovery：

```sh
gh workflow run "Recover and verify existing W103D kernel artifact" \
  --repo Remix123/w103d-armbian-usb-burn \
  -f source_run_id=36669269184 -f expected_head_sha=<原compile run的40位head SHA>
```

Recovery ID 必须等该 recovery run 成功后再传给 system build。以上编号是本次已知历史 run；复用已有的成功 system run 做最终重打包时，第四条直接引用它即可，不必重复编译内核或重建系统。

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

ZIP delivery run 会按 USB artifact ID 下载 raw IMG，并对来源 run、校验报告、实际 basename、size/SHA 与完整 provenance 逐项核验。它创建 `W103D_Armbian_{armbian_version}_{kernel_version}_USB_Burning_Tool.zip`，包内只有一个对应 `.img`、根目录 `SHA256SUMS`、provenance 和验证报告；检查每个 ZIP member CRC、内部 SHA256SUMS 和唯一 IMG，然后上传 raw ZIP artifact 及独立 `<zip>.sha256` sidecar。入口 summary 提供 ZIP 和 sidecar 直链。当前 26.11.0 新格式 ZIP 尚未构建；完成后在此补入 run/artifact ID、大小和哈希。

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
- `reference.lock.json`、`sources.lock.json`、`kernel-input-manifest.json`、`base-image.lock.json`、`system-image-verification.json`、kernel recovery/source run provenance；
- Actions run URL、固定 source commit/tag、IMG/ZIP artifact ID、size、SHA-256 和成功结论。

验证必须涵盖：Amlogic 容器 decode/check；vendor payload hash；`image.cfg` 与容器项目检查；sparse header fields/chunk/image CRC/trailing EOF；FAT `fsck.vfat -n` 和全部 boot 文件回读；ext4 `e2fsck -fn`/label/read-only mount；Trixie `os-release`、Ophub `DISK_TYPE=emmc`、fstab、machine-id、D-Bus link、SSH key 清理、resize service；主线 boot-emmc 与 vendor fallback 的 U-Boot legacy CRC及源码映射；kernel config、kernel/DTB 所有字节、rootfs全部模块、initramfs全部模块；最终 artifact 重新下载后 bytes+SHA256。这里的 container/image.cfg 检查不等于验证设备 MBR 或 eMMC 起始偏移。

本地 `py_compile`/pyflakes/YAML 检查只检验脚本语法和静态问题；synthetic sparse 或 U-Boot header 测试不等于真实固件运行；GitHub `mkimage`、packer、mount/fsck 回读才是本次离线镜像验证。所有层次都不能代替刷机。

### 6.2 Windows USB Burning Tool 操作（只用于最终烧录）

Windows 只承担最后的用户侧导入和烧录，不参与编译、镜像挂载、解包、系统修改、重打包或验证。建议顺序：

1. 从成功的 one-click run summary 下载 ZIP artifact 和同一 run 的 `.sha256` sidecar；不要下载普通 Armbian `.img.gz` 或旧的 USB raw artifact 作为最终件。
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

如需重复打包完全相同的 source/kernel/system，请先复用已有成功 compile/recovery run 和 system run 的锁定 artifacts；它们明确绑定 commit 和 SHA。重新运行 resolver 会重新选择届时的“最新”版本，不能把新 run 称为同一次版本复现。需要更新最新上游时才重新 dispatch 编译，然后：

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
| kernel compile失败 | compile step、source SHAs、config/toolchain 哈希 | 修复后产生新 run；不把中间 artifacts 当成功 |
| 只有 `rg: command not found` 导致 verifier失效 | 源 run jobs与失败日志，确认compile/upload均成功 | 可用严格 recovery workflow复核既有 artifact；其他failure不允许恢复捷径 |
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
- 可追溯 workflow：[reference-preflight.yml](../.github/workflows/reference-preflight.yml)、[compile-kernel.yml](../.github/workflows/compile-kernel.yml)、[recover-kernel-artifact.yml](../.github/workflows/recover-kernel-artifact.yml)、[build-system.yml](../.github/workflows/build-system.yml)、[build-usb-burn-image.yml](../.github/workflows/build-usb-burn-image.yml)。

没有移植旧手册 macOS `build_w103d_macos.py`、Windows/Cygwin `build_w103d_windows.py`、Cygwin e2fsprogs/mtools toolchain 或历史恢复脚本。Windows 只保留第 6.2 节 USB Burning Tool 的使用说明。原旧手册和相关旧脚本在修改前已有只读副本保存在本工作目录的 `work/original-guide-snapshot/`，用于回溯旧启动链、rootfs 和设备验收注意事项；snapshot 不是本次新的构建证据。
