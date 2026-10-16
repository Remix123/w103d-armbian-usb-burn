# W103D 双驱动工作流审计与本地修复（2026-10-16）

审计基线：`Remix123/w103d-armbian-usb-burn` main，提交 `ef0a93b1798d21f74108a6619ea3242947e6740f`。

用户确认：Stock 是锁定版本的 Ophub 原版驱动、不应用 v23 补丁；两个入口均使用 2021 N9 固件。本审计检查 Actions 编排与产物交接，不判断 v23 的实际无线性能。

## 实际失败记录

1. [内核运行 38722013475](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/38722013475)：6.18.56 编译和 DEB/runtime tar 打包完成，日志显示 `validated 7 files in .../6.18.56.tar.gz`，随后执行 `create_kernel_manifest.py --deb-dir ... --version ... --out ...` 时仅输出 `Usage: create_kernel_manifest.py <kernel_debs_dir>` 并退出 1。父运行 [38721808493](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/38721808493) 因此失败。这不是日志已证明的 v23 编译错误。
2. [系统运行 38719956114](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/38719956114)：下载成功的内核运行 `38719883426` 产物后，`prepare_kernel_input.py` 报 `expected exactly one 6.18.56.tar.gz in downloaded artifact, found 0`。父运行 [38719667876](https://github.com/Remix123/w103d-armbian-usb-burn/actions/runs/38719667876) 随后失败。旧缓存只包含 DEB/清单，缺少运行时 tar；基线提交补充了 tar 缓存路径，但没有使旧缓存失效。
3. 截至本次查询，Stock 工作流 ID `385404364` 的运行列表为空。因此下面 Stock 问题是源码和接口核对确认，不能写成 Stock 实跑成功或某次日志已复现。

## 发现和修复

| 优先级 | 问题及影响 | 本地修复 |
|---|---|---|
| P1 | 清单脚本被 rolling 入口改成仅接受位置参数，并改变 JSON schema，固定构建工作流仍传命名参数；即使绕过参数检查，下游仍需要 `kernel_release` 和 DEB metadata | 恢复命名接口和原 schema，同时兼容 rolling 位置参数；校验 ARM64、准确 DEB 版本、meson64 DTB，并记录 runtime tar 哈希 |
| P1 | 旧缓存缺 runtime tar，却可按原键命中并直接跳过编译；最近系统失败就是此症状 | v2 完整键使旧缓存失效；精确恢复，命中后核对 source lock、DEB、runtime tar 及内部 checksums |
| P1 | Stock 只有 `actions: read`，无法 dispatch 子 workflow；system/USB/ZIP 的输入名错误或缺少必需 run ID | Stock 与 v23 共用 `orchestrate_build.py`，授予与既有 v23 相同的编排权限，串行传明确 run ID |
| P1 | Stock 用 `gh run list --limit=1` 选择运行，无分支、HEAD、pipeline token 约束，无限轮询 | 两版共用已有严格关联、全局/阶段 deadline、取消和 provenance 逻辑 |
| P1 | 空 `kernel_patches: []` 被 `lock_entries()` 和最终 USB verifier 当成缺失；Stock 后续必失败 | 区分“键缺失/类型错误”和合法空列表；按 Stock/v23 配置分别校验 |
| P1 | compile 后 `verify-applied` 未传所选配置，Stock 会按默认 v23 集核对 | 预检、apply、verify-applied 都使用同一 `patch_config` |
| P1 | USB 上传 artifact 名固定为 `w103d-usb-burning-tool-image-ID`，ZIP 却要求 artifact 名以实际 `_USB_Burning_Tool.img` 结尾 | 从独立验证报告输出真实 basename，上传、readback 和 ZIP 消费统一 |
| P1 | 两种内核写入同一个 `kernel_stable/<version>.tar.gz`；同版本实际驱动不同，后一次覆盖前一次 | 分为 `kernel_stable_v23` / `kernel_stable_stock`，系统元数据分别指向对应 feed |
| P1 | 内核 feed 只有 runtime tar，Ophub 镜像默认 `DOWNLOAD_TYPE=deb`，在线更新会寻找未发布的 deb 包 | 最终 rootfs 设置 `DOWNLOAD_TYPE=tar`，独立回读验证 feed 类型与所选驱动一致 |
| P1 | 成品标签未区分驱动，且清理所有 `w103d-usb-` 旧 release；加入 Stock 发布后会删除另一版 | 标签含驱动、父 run、attempt；上传验证后仅清理同类型旧版本，保留其他类型及旧版无类型标签 |
| P2 | 缓存键 hash 了不存在的 `config/kernel.config`，漏了实际下载的配置哈希和 Ophub 构建提交；前缀恢复还会带入旧产物 | 键取完整固定来源及构建/验证代码内容，去掉模糊恢复 |
| P2 | `reuse_system_run_id` / `reuse_usb_run_id` 只检查成功和 workflow，没有检查驱动 | 把预期配置传给 system、USB、ZIP，消费/复用均校验 source lock 中配置和补丁哈希；发布前再次核验 ZIP 内 source lock |
| P2 | `pipeline_id in title` 让 attempt 1 可匹配 attempt 10，影响选择或取消 | 按完整 token 边界匹配，编排与清理共用 |
| P2 | `outputs-toolchain-version.txt` 诊断路径拼错；预检解析配置失败被 `|| echo ""` 吞掉；Stock 空集会 checkout 整棵内核 | 修复诊断路径；校验允许的配置、异常立即失败；空集只稀疏检出 Makefile |
| P2 | WORKFLOWS.md 指向不存在入口，称两版可共享编译产物，并把固件与驱动混称 | 重写入口、依赖、缓存和固件说明 |

## 验证方式与边界

- 16 项本地回归测试：两种驱动的锁及产物交接；Stock check/apply/verify-applied；错驱动/过期补丁拒绝；清单新旧 CLI；缺失或篡改 tar/DEB 拒绝；缓存来源变化；父子输入契约；raw IMG artifact 命名；token attempt 边界；独立 release 标签；tar feed 设置。
- YAML 解析、各工作流内嵌 bash 的 `bash -n`、Python AST 及 `git diff --check`。
- actionlint 1.7.12 检查本次六个相关工作流，关闭未安装的 shellcheck。该版本尚未收录 `ubuntu-26.04-arm`，针对这一个 runner-label 提示作精确忽略；实际上述日志已证明该 runner 正常启动。
- 固定 Ophub 提交 `556c02f3f30ae1f17ae5a025033a735936f5d8cf` 的 rebuild 支持自定义 kernel_usage，并将其写入 KERNEL_TAGS；已阅读对应更新脚本的 tag/URL 拼接逻辑。未执行真实 feed 发布、全量系统重建或设备在线升级。
- 原已有未跟踪的 `scripts/w103d-n9-transaction-review.py` 保留，不属于本次修复。

本次修复保存在本地 `codex/workflow-driver-audit` 分支。未推送 GitHub、未触发重新编译、未发布/删除远端 Release，也未改变 v23 补丁内容和固件二进制。远端 main 在部署修复前仍有上述问题。

## 部署后的验收顺序

1. 先跑本地回归检查与 Actions 语法检查，再把修复提交合入用于 dispatch 的分支；已有父运行会检测分支移动，应在旧运行结束后更新。
2. 分别运行 Stock 与 v23 父入口。首次使用 v2 缓存键会完整编译，此为旧残缺缓存失效的预期行为。
3. 核对每个父运行关联的 compile/system/USB/ZIP ID，核对 source lock 中补丁集（Stock 为 `[]`，v23 为实际 v23 文件哈希）。
4. 确认两版成功后的 feed、Release 标签、ZIP 内 provenance 和 rootfs DOWNLOAD_TYPE；确认缓存第二次精确命中时仍包含 runtime tar。
5. 对各版独立做 USB Burning Tool 导入及实机验收。在线更新后的实际 DTB/内核/initramfs 启动路径属于单独实机验收项，本次模拟产物测试不证明它们能启动。

## 范围外的入口

`build-latest-ophub.yml` 是第三个独立 rolling 入口，不是用户指定的两个入口。此次保留其清单脚本位置参数兼容；该入口另有输出目录、内核产物传递和装配接口差异，不能把此次六个工作流检查结果解释成 rolling 全链路已通过。
