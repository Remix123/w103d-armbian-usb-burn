# W103D Armbian USB Burning Tool 镜像构建与验收手册

**适用对象：ZTE W103D / Amlogic S905L3A。** 本流程在 GitHub Actions/Linux runner 上完成内核编译、Trixie 系统重建、参考镜像解析、USB Burning Tool 镜像封装和离线回读。最终交付是一件可导入 Amlogic USB Burning Tool 的原始 `.img` 文件；普通 Armbian 磁盘镜像和 ZIP 都只是中间输入，不能充当最终产物。

本流程以 [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian) 当前支持 W103D 的源代码和内核为系统来源。参考分支 [pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18](https://github.com/pigeon2049/amlogic-s9xxx-armbian/tree/w103d-burn-6.18) 仅用于理解板级 USB 烧录打包方法；不得从该分支替换系统或内核来源。

> 结果边界：GitHub 工作流只能给出来源可追溯的离线镜像和结构验证。没有实际用 USB Burning Tool 导入、没有刷写 W103D、没有串口冷启动证据时，不能宣称硬件适配成功。

## 1. 当前执行状态

仓库：[Remix123/w103d-armbian-usb-burn](https://github.com/Remix123/w103d-armbian-usb-burn)。本次最终打包使用代码提交 `025700e81035f16d70d3479aa90b9b917a04bbb9`。2026-09-30 已得到这些真实结果：

| 阶段 | GitHub run | 结果 |
|---|---|---|
| 参考包云端预检 | [36664112046](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36664112046) | 成功。ZIP/IMG 哈希、容器和参考布局解析通过 |
| 内核构建 | [36669269184](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36669269184) | 内核已编译成功；最初仅因验证 step 缺 `rg` 退出失败 |
| 已编译内核恢复校验 | [36680464101](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36680464101) | 成功；复核既有 artifact，没有重新编译 |
| Trixie 系统重建 | [36683231196](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36683231196) | 成功；系统内核、DTB、modules 与锁定内核产物逐字节比对通过 |
| USB Burning Tool 组装 | [36686786094](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094) | 成功；GitHub 真实 mkimage parser 测试、Amlogic pack、独立回读及 artifact 下载 SHA 校验通过。产物为 artifact `11084660823`，1,717,797,104 bytes，SHA-256 `6464978a0ccac8f5f228429a2a2860fc20c1203f1dfbbeeb295d10b243e35aec` |

本次最终工作流已成功，raw IMG artifact 重新下载后的大小和 SHA-256 一致，故可报告“GitHub 离线打包和验证通过”。设备烧录、冷启动、在线升级仍是独立的硬件验收，尚未执行。

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

`resolve_sources.py` 在编译 job 中查询 Ophub 的 W103D 设备记录、当前支持内核系列、内核配置与上游资产，再将解析结果固定到该次 `sources.lock.json`。后续 clone/checkout 使用锁中的 commit，而不是 `main`。`resolve_base_image.py` 选官方 Armbian archive 中的通用 Trixie minimal 基础镜像及其官方 SHA sidecar；该脚本生成的 `base-image.lock.json` 作为成功系统 artifact 保存，并随最终交付保留。生成逻辑见 [`resolve_base_image.py`](../scripts/resolve_base_image.py)。Ophub lock source commit 用于检出其 rebuild Action 和 `boot-emmc.cmd`。

当前已成功系统 run 对应的锁值是 build artifact 的权威记录。该次决策使用 Ophub `8b601ee74525a5cc68661d372c613666b9fafddd`，kernel tree `ophub/linux-6.18.y` commit `0f189d6b3197b94a8fbc96a670f0095cd63ce1a9`，kernel config metadata commit `4dbcaf0f83d63bdf4efd343a3af922b2866408f2`，版本 `6.18.54`、release `6.18.54-ophub`。下次构建需以新 run 的 sources lock 为准，不把此版本写死为“最新”。

Amlogic packer 在 [toolchain.lock.json](../config/toolchain.lock.json) 中固定：Khadas `utils` commit `a3604ed6d6863d1946d26d0ac8e765aef6f17430`，`aml_image_v2_packer` SHA-256 `8123b1295abb3262c76b650ba024975e38aedfd49b19f59a09f5920738ef1597`。它是静态 ELF32 i386，需 GitHub x86_64 Linux runner 的 IA32 执行支持。USB job 的真实 packer `-c`、`-d`、`-r` 结果由 run 日志证实；本地 macOS 不运行该工具。

### 3.3 目录和脚本职责

- `.github/workflows/reference-preflight.yml`：参考 ZIP 云端解压、Amlogic packer 预检、镜像布局/DTB/boot 链分析。
- `.github/workflows/compile-kernel.yml`：源解析与 arm64 内核编译；不会在系统重建时重新取 floating kernel。
- `.github/workflows/recover-kernel-artifact.yml`：仅针对已编译成功、后续验证工具缺失的既有 run 做来源核验和 artifact 恢复，不会重编。
- `.github/workflows/build-system.yml`：采用官方通用 Trixie base，经锁定 Ophub source Action 针对 W103D 重建，强制消费 kernel archive 并比较实际镜像内容。
- `.github/workflows/build-usb-burn-image.yml`：检查成功 system run、取 source lock/base lock/kernel artifact/参考 ZIP，装配 Burning Tool IMG 并独立检查。
- `scripts/validate_reference_zip.py`：安全校验并在 Actions 临时目录解 ZIP。
- `scripts/prepare_kernel_input.py`、`scripts/create_kernel_manifest.py`、`scripts/recover_kernel_artifact.py`：提取、核验 kernel build archive 和 provenance。
- `scripts/assemble_usb_image.py`：组合 FAT/ext4 和保留厂商组件；仅由 GitHub Ubuntu runner 用 `sudo python3` 执行。
- `scripts/verify_usb_image.py`：再次解开最终 Amlogic IMG，完整回读 sparse、boot 文件、rootfs、kernel/DTB/modules、CRC 和文件系统。

## 4. GitHub Actions 操作顺序

仓库默认分支 `main`。使用 Actions 页面 **Run workflow** 手动启动；必须选正确分支，记录 run URL。不要从本地下载 IMG 后解包或构建，也不要用本地 Actions runner 替代云端镜像处理。

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

工作流会严格识别来源 workflow path/name，下载其中唯一的 sources lock、kernel manifest 和 `${version}.tar.gz`，用 archive SHA 校验后发布到 `kernel_stable` Release。发布时先检查并串行化同版本资产，再在官方 Armbian archive 中解析通用 Odroid N2 Trixie minimal base 及 SHA sidecar。之后以锁定 Ophub Action 和 board `s905l3a-w103d` 重建系统，关闭自动替换 kernel；检查唯一符合名称的 Trixie W103D Server/minimal 输出、`/etc/os-release`、`/etc/ophub-release`、kernel、Linux DTB、完整 modules 内容。不能取到别的 board/series 的同版本 `.img.gz`。

成功 system artifact 名为 `w103d-rebuilt-system-<run_id>`，包含压缩磁盘镜像、`system-image-verification.json`、`base-image.lock.json`、`sources.lock.json`、kernel manifest 和 Ophub commit。另有 diagnostics 和 candidate artifacts；失败时 candidate 只是未验收输入，不能交付。

本次系统成功 run 为 [36683231196](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36683231196)。它给 USB workflow 提供可校验的 `.img.gz` 和 manifest。

### 4.4 生成 USB Burning Tool 单文件 IMG

启动 `Assemble and validate W103D USB Burning Tool image`，输入：

- `system_run_id`: 上一步成功系统重建的 run ID

此工作流拒绝非成功的系统 run，也校验 path/name、image report 和 kernel manifest/source lock 一致。它重新下载参考 ZIP、system artifact、kernel Release archive 及对应 Ophub boot-script source；在 runner 使用 `mkfs.fat`/mtools、e2fsprogs、`unmkinitramfs`、`mkimage` 和锁定 Amlogic packer。

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
7. 只在上述验证全部通过时上传一个 `.img` 文件。上传后按 artifact ID 查询真实 metadata，以 IMG basename 校验；从 GitHub artifact download 接口读取，再判断响应是 raw bytes 还是 ZIP 包并提取唯一 IMG，核对下载副本 SHA-256 和 size 与原始 IMG 一致。

本次成功 USB run：[36686786094](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094)，head commit `025700e81035f16d70d3479aa90b9b917a04bbb9`。GitHub runner 上真实 `mkimage` 的 1/2/3/4-byte script 测试、镜像装配、独立回读、raw IMG 上传和下载 bytes/SHA-256 校验均通过。最终 artifact ID `11084660823`，字节数 `1717797104`，SHA-256 `6464978a0ccac8f5f228429a2a2860fc20c1203f1dfbbeeb295d10b243e35aec`。验证报告统计 245 个 FAT regular files、3156 个 rootfs modules、538 个 initramfs modules 均通过；离线验证已通过，但未在实体 W103D 烧录或启动。

### 4.5 最终下载位置和 SHA 核对

Actions 成功后，在相应 run 的 **Artifacts** 下载 artifact。`archive: false` 请求单文件 raw 模式；以 artifact metadata 和最终 `artifact-download-validation.txt` 为准。因为服务端可能按文件 basename 命名，不要只看 YAML 的 `name:` 字符串。报告中至少应记录：artifact ID、实际 basename、byte size、SHA-256、source system/kernel/reference/base locks、最终 verifier JSON。本次成功成品为 artifact `11084660823`，文件 `W103D_Armbian_Trixie_Kernel-6.18.54_USB_Burning_Tool.img`，1,717,797,104 bytes，SHA-256 `6464978a0ccac8f5f228429a2a2860fc20c1203f1dfbbeeb295d10b243e35aec`；下载校验报告和 provenance 在 [本次 run](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094) 的 `w103d-usb-burning-tool-verification-36686786094` artifact 中。

目标为一件完整 `.img`，不是下载 artifact ZIP 本身。若 GitHub artifact 的真实结果仍被压缩成下载 ZIP，只能把 ZIP 当 HTTP/API 下载封装，内部必须恰有那一个原始 IMG；必须将 IMG 字节比对通过后，才能给出最终 `.img`。不得让用户手工拼接多个分卷，不得把 verification report 当作镜像。

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

只有在 USB workflow 全绿并且 artifact 回读通过，才归档以下文件：

- 单个 `W103D_Armbian_Trixie_Kernel-<version>_USB_Burning_Tool.img`；
- `SHA256SUMS` 和 `artifact-download-validation.txt`；
- `verification-report.json`、`assembly-input-report.json`；
- `reference.lock.json`、`sources.lock.json`、`kernel-input-manifest.json`、`base-image.lock.json`、`system-image-verification.json`、原 system run JSON；
- Actions run URL、commit ID、实际 artifact ID 和成功结论。

验证必须涵盖：Amlogic 容器 decode/check；vendor payload hash；`image.cfg` 与容器项目检查；sparse header fields/chunk/image CRC/trailing EOF；FAT `fsck.vfat -n` 和全部 boot 文件回读；ext4 `e2fsck -fn`/label/read-only mount；Trixie `os-release`、Ophub `DISK_TYPE=emmc`、fstab、machine-id、D-Bus link、SSH key 清理、resize service；主线 boot-emmc 与 vendor fallback 的 U-Boot legacy CRC及源码映射；kernel config、kernel/DTB 所有字节、rootfs全部模块、initramfs全部模块；最终 artifact 重新下载后 bytes+SHA256。这里的 container/image.cfg 检查不等于验证设备 MBR 或 eMMC 起始偏移。

本地 `py_compile`/pyflakes/YAML 检查只检验脚本语法和静态问题；synthetic sparse 或 U-Boot header 测试不等于真实固件运行；GitHub `mkimage`、packer、mount/fsck 回读才是本次离线镜像验证。所有层次都不能代替刷机。

### 6.2 Windows USB Burning Tool 操作（只用于最终烧录）

Windows 只承担最后的用户侧导入和烧录，不参与编译、镜像挂载、解包、系统修改、重打包或验证。建议顺序：

1. 从成功的 USB workflow artifact 取得唯一原始 `.img`，另取 `SHA256SUMS`/下载校验报告；不要对镜像 artifact 手工重命名后再混淆来源。
2. 用 PowerShell 校验下载文件（把哈希替换成实际 Actions 报告值）：

   ```powershell
   Get-FileHash .\W103D_Armbian_Trixie_Kernel-6.18.54_USB_Burning_Tool.img -Algorithm SHA256
   ```

3. 保留原始可恢复参考包及其 SHA256；确认 USB 驱动、设备进入下载模式方式、供电和线缆。准备串口与已验证恢复手段。
4. 在与该盒子相匹配的 Amlogic USB Burning Tool 中导入最终 IMG，记录工具版本、导入结果和 log。不要导入 `.img.gz`、普通 Ophub 磁盘镜像、ZIP 或 artifact reports。
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
