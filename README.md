# W103D Armbian USB Burning Tool 镜像

本仓库使用 ophub 当前支持 W103D 的源码和内核，在 GitHub Actions 中编译内核、从 Ophub 官方 Releases 锁定最新 Trixie arm64 server 通用 `-trunk` 镜像后重建 W103D 系统，再与 W103D 参考包中的厂商组件组合。唯一入口是 **Build and deliver W103D USB image**；一次 dispatch 会在固定 source tag 上串行创建预检、内核、系统、USB 和 ZIP 多个 workflow runs。所有镜像操作与 ZIP 打包/回读均在 GitHub Actions 执行。

最终下载件是一个 ZIP，内含单个可导入 Amlogic USB Burning Tool 的原始 `.img`、`SHA256SUMS` 及 provenance/验证报告。IMG 和 ZIP 文件名由重建系统实际 `/etc/armbian-release VERSION` 与锁定内核版本共同生成；参考 IMG `W103D_Armbian_26.8.1_Server.img` 仅作为厂商启动组件参考，不决定系统版本。

历史版 GitHub 离线 IMG 打包和下载回读校验已通过；该旧 basename 未含系统版本，不作为新版 ZIP 交付件。Ophub 官方 26.11.0 base 的轻量系统重建现已成功：

- [Ophub 26.11.0 系统轻量验证](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36692133844)：复用 kernel recovery run `36680464101`，未重编内核；`/etc/armbian-release VERSION=26.11.0`、内核 `6.18.54-ophub`、DTB 和 modules 验证通过。新版 USB/ZIP 仍待云端验证。

最终 basename 格式为 `W103D_Armbian_<实际系统版本>_<内核版本>_USB_Burning_Tool.img/.zip`；系统与内核版本来自锁定 run 的实际报告，不从参考包名称猜测。

完整操作步骤、输入 ZIP 校验、唯一入口和恢复参数、子 workflow/run 关联、内核与系统来源锁、板级启动链、ZIP 内外验证、故障处置及 Windows 烧录说明见[中文完整手册](docs/W103D_USB_Burning_Tool_打包完整手册.md)。Windows 只用于最后解 ZIP、校验并导入 `.img`；尚未在实体 W103D 上刷写或启动，不能视为硬件验收。
