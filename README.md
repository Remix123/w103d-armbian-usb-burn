# W103D Armbian USB Burning Tool 镜像

本仓库使用 ophub 当前支持 W103D 的源码和内核，在 GitHub Actions 中重建 Trixie 系统，并将其与 W103D 参考包中的厂商组件组合，输出可导入 Amlogic USB Burning Tool 的单个原始 `.img`。参考 IMG 只以单文件 ZIP 存在 GitHub Release；所有镜像解包、挂载、修改、封装和回读验证均在 GitHub Actions 执行。

本次已通过 GitHub 离线打包和下载回读校验：

- [成功的 USB 打包工作流](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094)
- [最终 raw IMG artifact](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094/artifacts/11084660823)：`W103D_Armbian_Trixie_Kernel-6.18.54_USB_Burning_Tool.img`，1,717,797,104 bytes，SHA-256 `6464978a0ccac8f5f228429a2a2860fc20c1203f1dfbbeeb295d10b243e35aec`
- [验证报告与来源锁 artifact](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/36686786094)

完整操作步骤、输入 ZIP 校验、五个 workflow 的运行顺序、内核与系统来源锁、板级启动链、镜像验证、故障处置及 Windows 烧录说明见[中文完整手册](docs/W103D_USB_Burning_Tool_打包完整手册.md)。Windows 只用于最后导入/烧录；本次尚未在实体 W103D 上刷写或启动，不能视为硬件验收。
