# W103D Armbian USB Burning Tool 镜像

本仓库目标是在 GitHub Actions 中编译 ophub 支持 W103D 的 Linux 内核、构建 Armbian 系统、保留设备厂商烧录引导组件，并打包为单个 USB Burning Tool 可导入的 Amlogic `.img`。普通 Armbian 磁盘镜像只是中间输入，不能代替最终线刷容器。

参考输入通过专用 GitHub Release 保存为 ZIP，不进入 Git 历史。ZIP 内只允许一个文件 `W103D_Armbian_26.8.1_Server.img`。Actions 校验 ZIP 和 IMG 的 SHA-256 后才会在 GitHub runner 解压和分析参考包。

当前已准备轻量 `Reference image preflight` 工作流，用来在 GitHub runner 校验参考 ZIP、Amlogic 容器和 packer。完整内核/系统构建工作流须在评审源码锁定和内核产物接入方案后启用。任何离线镜像校验均不等同于 USB Burning Tool 实际烧录或设备冷启动验收。

完整操作、构建边界和 Windows 烧录说明见 [`docs/W103D_USB_Burning_Tool_打包完整手册.md`](docs/W103D_USB_Burning_Tool_打包完整手册.md)。
