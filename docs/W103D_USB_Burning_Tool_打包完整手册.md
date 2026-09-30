# W103D Armbian USB Burning Tool 镜像构建手册

**目标：所有固件镜像解包分析、内核编译、系统构建、线刷打包和离线验证均在 GitHub Actions/Linux runner 完成。最终交付必须是单个可导入 USB Burning Tool 的 Amlogic `.img`。** 普通 Armbian 磁盘 `.img`、ZIP 或分卷文件都不是最终交付。

尚未运行完整构建，也没有 USB Burning Tool 导入、设备烧录或冷启动记录。GitHub 预检通过只证明输入容器可读取、静态组件检查通过。

## 1. 输入与仓库布局

指定参考文件为 `W103D_Armbian_26.8.1_Server.img`。本次原文件大小为 1,919,320,976 字节，SHA-256 为：

```text
60b0c3355eda080cdf0c350e063bb6c8ac664f0dea872f5d5377d8a53503159d
```

先将该 IMG 压缩为 ZIP，ZIP 内只包含该 IMG，且不保留父目录路径。ZIP 存在专用 Release `reference-input-2026-09-30`，不提交 Git。配置中的锁值同时包含 ZIP SHA-256、成员文件名、IMG 大小及 IMG SHA-256。下载后先验 ZIP 哈希，再检查 ZIP 只有预期成员、拒绝绝对路径和 `..`，最后流式解压并复核 IMG 大小和哈希。任何一项失败均停止。

`Reference image preflight` 是唯一允许在评审前运行的工作流。它在 GitHub runner 下载 ZIP、校验后解压到 runner 临时目录、运行 Khadas Amlogic packer 容器检查并保存小型验证报告。它不会把参考 IMG 上传为构建产物。镜像分区、布局和板级组件清单必须从 Actions 输出生成后再审查；不能从旧手册偏移值推定新包相同。

## 2. 来源和可复现构建

系统配置从 ophub 主仓库 `ophub/amlogic-s9xxx-armbian` 的 W103D 板型记录解析，目标为 `s905l3a-w103d`，Linux DTB 为 `meson-g12a-w103d.dtb`，支持内核系列由当次上游记录决定（目前记录为 `stable/6.18.y`）。pigeon 的 `w103d-burn-6.18` 仅作为线刷组装设计参考，不作为系统或内核源代码。

每次完整构建开始时生成来源锁文件，记录并实际用于 checkout 的 ophub 仓库提交、kernel 仓库提交、内核版本、构建脚本、工具及容器 digest。只记录 SHA 而工作流仍 `clone main`、`uses @main` 或跟随浮动镜像标签不算锁定。上游标签按构建开始时解析一次，后续 job 只使用锁定值。

现有 ophub `recompile` 流程可使用预先 checkout 到 `compile-kernel/linux-6.18.y` 的精确内核树，并以 `auto_kernel=false` 防止更新分支；编译结果是 ophub 内核 `.deb`。系统构建必须显式安装本次 `.deb`，并校验 boot、DTB、modules 与本次编译输出相符。不能让 rebuild 动态下载另一个 release kernel 包后仍把它称为本次编译内核。

建议阶段划分：源码解析与锁定 → W103D 内核编译 → 下载当前 ophub W103D Server 基础磁盘镜像并替换为锁定内核包 → 在 GitHub runner 解包指定参考镜像 → 依据实测布局组装 bootfs/rootfs 与厂商分区 → sparse 往返 → Amlogic 容器封装 → 从最终 IMG 独立复核 → 上传最终单个 IMG 和报告。

内核编译使用 `ubuntu-26.04-arm` runner，匹配 ophub kernel toolchain 的 arm64 容器。参考包校验/打包使用 Linux runner；Khadas 工具必须按 `toolchain.lock.json` 校验版本、哈希和 `file` 架构。当前锁定的 Linux x86 packer 是静态 ELF32 i386，因此打包 runner 需能执行 IA32 binary；预检工作流将实际调用以确认兼容。runner 兼容验证未通过前不可进入打包。

## 3. ZIP 参考包处理

ZIP 解压仅发生在 GitHub Actions。构建脚本应复用 `scripts/validate_reference_zip.py` 的安全检查：比较 ZIP 哈希，限制成员数量为一，检查成员路径和文件名，检查 uncompressed size，流式解压并比较 IMG SHA-256。解压目录使用 runner 临时目录，禁止覆盖现有文件。后续解包工具对参考容器执行只读式读取；所有保留组件都生成大小和 SHA-256 清单。

旧手册中的分区偏移、bootfs 容量、文件名、组件哈希只作为待核对线索。只有 GitHub Actions 从本次 IMG 生成布局报告后，才可把通过审查的值写入 `reference-manifest.json` 并用于组装。

## 4. 最终线刷镜像构建

打包流程参考 pigeon 的 `board/w103d/burn/assemble.sh`，但必须根据新参考包和本次内核实际版本参数化，不能沿用固定 `6.18.52`、旧目录路径或未验证的 1 GiB bootfs。组装顺序为：

1. 解压 ophub Server 磁盘镜像并核对其系统、分区与锁定构建记录。
2. 安装本次编译出的 kernel package，确认内核、DTB、模块 vermagic、initramfs 一致。
3. 依据参考布局建立 FAT bootfs 与 ext4 rootfs；设置 `uEnv.txt`、U-Boot 脚本、根文件系统标签、`fstab` 和首次扩容逻辑。
4. 保留经审查的厂商 DDR、USB U-Boot、厂商 DTB、boot/recovery、dtbo、vbmeta、logo、platform 配置；每个保留项逐字节校验。
5. 清除机器身份和 SSH host keys，配置首次启动生成。
6. 运行文件系统检查，将 raw 文件系统转换 Android sparse 后解码回读，比较哈希。
7. 依据参考包的 `image.cfg` 结构将全部 payload 交给锁定的 Amlogic packer，生成唯一最终 `.img`。

当前参考容器内部布局尚未由 GitHub preflight 产出，所以不能把旧包布局硬编码为已核实事实。任何组件不匹配或内核包无法注入时应失败关闭。

## 5. 成品验证和交付

验证 job 重新获取完整的最终 `.img`，检查 Amlogic 容器结构、CRC/分区校验、所有保留组件哈希、sparse 解码内容、FAT/ext4 文件系统、启动路径、根标签、模块 vermagic 和来源清单。验证失败不上传成品。

至少交付以下单文件及小型伴随记录：

```text
W103D_Armbian_<system>_<kernel>_USB_Burning_Tool.img
SHA256SUMS
sources.lock.json
reference-validation.json
verification-report.json
```

最终 `.img` 使用 `actions/upload-artifact` 的单文件直传模式 `archive: false`，这样 artifact 内容应是原始 IMG。实施时必须在真实 GitHub run 中检查下载字节与报告的 SHA-256 相同；不能只依据 YAML 推断下载结果。如果 artifact 上传/下载对目标大小不支持，停下来报告并评估 GitHub Release 是否满足单个附件限制，不能以 ZIP 或分卷交付替代 IMG。ZIP 只用于参考输入上传。

离线验证通过只说明镜像结构和内容符合脚本检查。Windows USB Burning Tool 导入/烧录、W103D 冷启动、串口、根挂载、扩容、网络和无线仍需设备实测；不得将 Actions 成功描述为硬件验收通过。

## 6. Windows USB Burning Tool 使用

构建和打包都不在 Windows 执行。Windows 仅用于最终使用：从 GitHub 下载完整 `.img` 和 `SHA256SUMS`，用 PowerShell `Get-FileHash -Algorithm SHA256` 对照报告；启动与该盒子兼容的 USB Burning Tool，将最终 IMG 导入，并记录工具版本与导入结果。先确认设备进入烧录模式的方法、供电、线缆和恢复参考包。擦除选项依照该设备已验证流程，不从本手册推断通用擦除设置。

首次刷写需准备 UART 日志和已验证的恢复方法，记录 USB 下载、厂商启动链、二级 U-Boot、Linux、根挂载及首次扩容结果。实际烧写和设备验收目前尚未执行。

## 7. 失败处理

哈希、ZIP 成员、源提交、文件系统检查、模块版本、sparse 回读、packer 校验或最终容器校验任何失败都应停止，保存 Actions 日志和失败报告。修复后产生新的 workflow run 和来源锁文件；不要在旧验证报告上改写状态，也不要把前次产物与新输入混用。
