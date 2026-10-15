# W103D Armbian Build System

本仓库包含两个完整的 W103D USB 烧录镜像构建流程：

## Workflows

### 1. Build and deliver W103D USB image (v23 driver)
**文件**: `.github/workflows/orchestrate_build.yml`

使用改进的 v23 Wi-Fi 驱动（包含 reason=4 空闲断连修复补丁）。

**特性**:
- 应用 W103D 专用内核补丁（包括 v23 Wi-Fi 驱动修复）
- 内核版本：动态从 ophub 获取（当前 6.18.y）
- 内核编译缓存：基于 kernel_sha + config + 补丁 + 工具链构建 cache key

**触发**: 手动触发，选择 `run_all_jobs=true`

### 2. Build and deliver W103D USB image (Stock 2021 driver)
**文件**: `.github/workflows/build-usb-stock-driver.yml`

使用原版 2021 Wi-Fi 驱动（无 v23 补丁）。

**特性**:
- **不应用** W103D v23 补丁（使用 `kernel-patches-stock.json`）
- 内核版本：动态从 ophub 获取（当前 6.18.y）
- 与 v23 workflow **共享缓存**（当 kernel_sha/config/toolchain 相同时）
- 适用于需要原版驱动行为的场景

**触发**: 手动触发，选择 `run_all_jobs=true`

## 缓存机制

两个 workflow 共享内核编译产物缓存：

**Cache Key 构成**:
```
w103d-kernel-{kernel_sha}-{hashFiles(config, patch_config, patches)}-{toolchain_sha256}-{container_digest}
```

**特点**:
- 当 `patch_config` 不同时（v23 vs stock），生成不同的 cache key
- 当 kernel/config/toolchain 相同但补丁不同时，各自独立缓存
- 使用 `restore-keys: w103d-kernel-` 前缀匹配最近缓存
- GitHub Actions cache 限制 10 GB，实际单份内核产物约 300-500 MB

**缓存命中**:
- ✓ 完全命中：跳过 76 分钟内核编译
- ✓ 部分命中：从最近构建恢复部分产物
- ✗ 未命中：完整编译（首次构建或 config 变更后）

## 内核版本选择

两个 workflow 都动态从 ophub 的 `model_database.conf` 获取 W103D 支持的内核系列。

**当前配置** (2026-10-15):
- W103D 对应内核系列: `stable/6.18.y`
- 自动跟踪 ophub/linux-6.18.y 的 main 分支最新提交

如果 ophub 未来更新 W103D 到 6.6.y 或其他系列，两个 workflow 都会自动跟随。

## Wi-Fi 固件

MT7663 固件文件已提交到 `firmware/mediatek/`:
- `mt7663_n9_v3.bin` (431 KB)
- `mt7663pr2h.bin` (272 KB)

USB 镜像组装时直接从仓库读取，无需解包参考镜像（但参考镜像仍保留用于容器结构）。

## 构建流程

两个 workflow 都按相同顺序调用子 workflow：

1. **Compile kernel** (`compile-kernel.yml`)
   - 解析并锁定内核源码 SHA
   - 检查并应用补丁（v23 或 stock）
   - 缓存检查：命中则跳过编译
   - 编译内核并打包 deb
   - 保存缓存

2. **Build rootfs** (`build-system.yml`)
   - 构建 Armbian 基础系统
   - 安装内核 deb 包

3. **Assemble USB image** (`build-usb-burn-image.yml`)
   - 安装 Wi-Fi 固件
   - 组装 USB 烧录镜像

4. **Create delivery package** (`deliver-zipped-image.yml`)
   - 打包最终可交付的 zip 文件

## 区别总结

| 特性 | v23 Workflow | Stock Workflow |
|------|--------------|----------------|
| 补丁配置 | `kernel-patches.json` | `kernel-patches-stock.json` |
| Wi-Fi 驱动 | v23 (reason=4 修复) | 2021 原版 |
| 内核版本 | 6.18.y (动态) | 6.18.y (动态) |
| 缓存共享 | 是（条件命中） | 是（条件命中） |
| 适用场景 | 日常使用（推荐） | 兼容性测试 |

## 开发注意事项

- 修改内核补丁时，更新 `config/kernel-patches.json` 或 `config/kernel-patches-stock.json`
- 修改内核配置时，更新 `config/kernel.config`
- 新增补丁文件放在 `patches/w103d/` 目录
- Cache key 会自动包含以上文件的 hash，变更后会触发重新编译
