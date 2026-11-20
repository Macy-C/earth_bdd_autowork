# 便携版浏览器目录 (`resources/browser/`)

本目录用于存放 **BDD Autowork 用例运行器（D1 Web UI）** 的内置便携版浏览器，确保在任何 Windows 测试机（含无外网、无系统 Edge/Chrome 的精简测试机）上都拥有完全一致、零差异的独立桌面窗口体验。

## 1. 固定查找优先级

`autowork_core/utils/debug_tools/feature_runner_web.py` 中的 `find_browser_executable()` 按以下固定顺序查找：

1. **项目内置便携版（最高优先级）**：
   - `resources/browser/current/thorium.exe`
   - `resources/browser/current/chrome.exe`
   - `resources/browser/current/msedge.exe`
   - `resources/browser/current/bin/thorium.exe`
   - `resources/browser/current/bin/chrome.exe`
   - `resources/browser/current/Application/chrome.exe`
2. **系统已安装浏览器（兜底回退）**：
   - Microsoft Edge (`msedge.exe`)
   - Google Chrome (`chrome.exe`)

一旦 `resources/browser/current/` 下存在上述任一可执行文件，用例运行器将**无条件固定使用内置便携版**，并自动传入轻量化低内存与 `--app` 独立无边框窗口参数，同时使用独立的浏览器 Profile 目录（`artifacts/.runner_webview_profile_thorium`）避免与系统浏览器配置冲突。

## 2. 当前内置版本与精简说明（< 100 MB）

- **上游来源**：`Alex313031/Thorium-Win` Releases (`https://github.com/Alex313031/Thorium-Win/releases`)
- **目录位置**：`resources/browser/current/`
- **真实目录大小（逻辑大小 & 占用空间）**：**92.57 MB**（严格控制在 `< 100 MB` 以内）
- **精简策略**：
  - 仅保留核心运行时文件（`thorium.exe`、`97.0.4690.0/chrome.dll`、`chrome_elf.dll`、`ffmpeg.dll`、`icudtl.dat`、`resources.pak`、`chrome_100_percent.pak`、`ui_resources_100_percent.pak`、`v8_context_snapshot.bin`、`mojo_core.dll`、`libEGL.dll`、`libGLESv2.dll`）
  - `Locales/` 仅保留 `zh-CN.pak` 与 `en-US.pak`（移除其余 50+ 国语言包）
  - 移除 `content_shell.exe`、`chromedriver.exe`、Widevine/ClearKey DRM、`nacl_irt_x86_64.nexe`、SwiftShader、默认扩展等非运行器必需组件
  - 清除 `chrome.dll` / `libGLESv2.dll` / `ffmpeg.dll` PE 头的 `GUARD_CF` 与 `LoadConfig` 目录项后应用 `upx -9` (NRV2E) 无损可执行文件压缩，将真实文件大小压缩至 **92.57 MB**，同时保持快速解压启动与稳定性
