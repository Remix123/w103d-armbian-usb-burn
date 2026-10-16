# W103D Armbian USB Burning Tool 镜像

本仓库使用 ophub 当前支持 W103D 的源码和内核，在 GitHub Actions 中编译内核、从 Ophub 官方 Releases 锁定最新 Trixie arm64 server 通用 `-trunk` 镜像后重建 W103D 系统，再与 W103D 参考包中的厂商组件组合。入口为 **Build and deliver W103D USB image**（v23）和 **Build and deliver W103D USB image (Stock 2021 driver)**（Ophub 原版驱动）；一次 dispatch 会在触发分支上串行创建预检、内核、系统、USB 和 ZIP 多个 workflow runs，最后发布 Release。所有镜像操作与 ZIP 打包/回读均在 GitHub Actions 执行。

最终下载件是一个 ZIP，内含单个可导入 Amlogic USB Burning Tool 的原始 `.img`、`SHA256SUMS` 及 provenance/验证报告。IMG 和 ZIP 文件名由重建系统实际 `/etc/armbian-release VERSION` 与锁定内核版本共同生成；参考 IMG `W103D_Armbian_26.8.1_Server.img` 仅作为厂商启动组件参考，不决定系统版本。

构建会：

- 拉取 Ophub 最新源码、W103D 当前使用的最新 stable 内核分支（`ophub/linux-6.18.y`）和 `ophub/kernel` 的内核配置，锁定为具体 commit；
- v23 入口应用 `config/kernel-patches.json` 中的补丁并预检；Stock 入口使用 `config/kernel-patches-stock.json` 的空补丁集，不修改 Ophub 驱动；
- 使用参考包中的 2021 版 MT7663 N9 固件（`config/wifi-firmware.lock.json`）；
- 把最终 ZIP 发布为 GitHub Release，分别保留两种驱动最新的一个：<https://github.com/Remix123/w103d-armbian-usb-burn/releases/latest>。

最终 basename 格式为 `W103D_Armbian_<实际系统版本>_<内核版本>_USB_Burning_Tool.img/.zip`；系统与内核版本来自锁定 run 的实际报告，不从参考包名称猜测。

完整操作步骤、输入 ZIP 校验、唯一入口和恢复参数、子 workflow/run 关联、内核与系统来源锁、板级启动链、ZIP 内外验证、故障处置及 Windows 烧录说明见[中文完整手册](docs/W103D_USB_Burning_Tool_打包完整手册.md)。Windows 只用于最后解 ZIP、校验并导入 `.img`。每个新内核版本的首个镜像建议上板做一次 Wi-Fi 冒烟测试。

两种入口、驱动身份/缓存隔离、恢复参数和独立内核源说明见 [WORKFLOWS.md](WORKFLOWS.md)；2021 指固件版本，两版都使用该固件。最新编排审计见 [2026-10-16 审计报告](docs/workflow-audit-2026-10-16.md)。
