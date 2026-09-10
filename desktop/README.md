# 摸鱼模式 · 透明常驻悬浮窗（desktop/）

> 对应设计方案 §11。独立 `desktop/` 工程，**与 English OS 本体零耦合**：
> 删掉这个目录 / 不打包即回退，不影响本体任何功能。

## 先说清楚：要不要「下载」？

**不是下载一个现成的 .exe / .dmg。** Tauri 应用是按你的操作系统在你**自己电脑**上编译出来的，
没法跨系统通用。你要「准备」的是三样东西：

1. **装一次编译环境**（Rust 工具链 + 系统 WebView）—— 一次性，几分钟。
2. **拿这份代码**（git clone 或 GitHub 网页 Download ZIP）。
3. **跑一条命令** `tauri dev` → 立刻弹出透明悬浮窗；想长期使用再 `tauri build` 出安装包。

> 窗口里装的是线上 Render 的 `/stealth` 页面，所以**前提**：
> Render 服务 `english-os` 已部署、且环境变量 `ARK_API_KEY` 已填（才有 AI 批改）。
> 断网时页面自动回落本地规则，分数旁标「本地估算，仅供参考」。

---

## 一、装编译环境（按你的系统选）

### Windows（最常见）
1. 装 [Rust](https://www.rust-lang.org/tools/install)（官网 `rustup-init.exe`，一路回车）。
2. 装 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)（Win10/11 大多已自带；没有就装一下）。
3. 装 Tauri CLI（管理员打开**终端**）：
   ```powershell
   cargo install tauri-cli
   ```
4. 装 [Visual Studio 生成工具](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，勾 **「使用 C++ 的桌面开发」**（编译 Rust 原生部分必需）。

### macOS
```bash
# 1) 装命令行工具（含 C 编译器）
xcode-select --install
# 2) 装 Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rust-lang.org | sh
# 3) 装 Tauri CLI
cargo install tauri-cli
```
> macOS 透明窗走私有 API，已在 `Cargo.toml` / `tauri.conf.json` 开好，无需你动。

### Linux（Ubuntu/Debian）
```bash
sudo apt update && sudo apt install -y libwebkit2gtk-4.1-dev build-essential \
  curl wget file libssl-dev libayatana-appindicator3-dev librsvg2-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rust-lang.org | sh
cargo install tauri-cli
```

---

## 二、拿代码 + 跑起来

```bash
# 1) 拿仓库（二选一）
git clone https://github.com/cand79234-source/engilsh-study.git
#    或 GitHub 网页点 Code → Download ZIP 解压

# 2) 进壳工程目录
cd engilsh-study/desktop/src-tauri

# 3) 开发模式：一条命令，立刻弹出透明悬浮窗（和正式窗口体验一致）
tauri dev

# 4) 想长期使用 → 出安装包（Windows=.msi / macOS=.dmg / Linux=.deb）
tauri build
#    产物在：src-tauri/target/release/bundle/
```

> 第一次 `tauri dev` 会下载并编译大量 Rust 依赖（几分钟到十几分钟，看网速），
> 之后就快了。编译完窗口自动弹出，贴在屏幕右上角。

---

## 三、换 Render 地址 / 图标

- **换后端地址**：改 `tauri.conf.json` 里 `app.windows[0].url`
  （以及 `app.security.dangerousRemoteDomainIpcAccess[0].domain` 同步改，否则远程页拿不到窗口 IPC，标题栏拖动会失效——不影响练习）。
- **换图标**：`cargo tauri icon 你的图标.png`（覆盖 `icons/`）。

## 四、工程结构

```
desktop/
  src-tauri/
    src/main.rs          # 极薄：只做「右上角定位」，窗口全在 conf 里声明
    tauri.conf.json      # 主窗口 = 外部 URL + 透明 + 无边框 + 置顶 + IPC 白名单
    capabilities/default.json   # 只开窗口级权限（拖动/置顶），不开文件与 shell
    build.rs
    icons/icon.png
  dist/.gitkeep          # frontendDist 占位（壳加载远程页面，无本地前端）
```

## 五、已知边界

- `transparent(true)` 在 Windows / Linux 直接可用；macOS 走私有 API（已开）。
- 图标先用本体 icon 顶上，可随时 `cargo tauri icon` 换。
- 以后要「秒开」再考虑把 Python 后端打进安装包（混合回落，§11.5）。

---

## 六、不想装环境？用 CI 出安装包（零安装，推荐）

本机不想装 Rust / WebView？仓库根目录已放 `.github/workflows/release.yml`，
**推上去后 GitHub 在云端自动编译**，你只去 Releases 下载对应系统的包直接装：

1. 把工程推到 GitHub（含 `.github/workflows/release.yml`）。
2. GitHub → Actions → 选 `Build eos-float desktop` → `Run workflow`
   （填版本号如 `v1.0.0`），或本地 `git tag v1.0.0 && git push --tags`。
3. 等几分钟，去 **Releases** 下载：
   - Windows → `eos-float_*.msi`
   - macOS → `eos-float_*.dmg`
   - Linux → `eos-float_*.deb` / `.AppImage`

> **未签名提示**（你暂无签名证书，正常现象）：
> - Windows 装 .msi 时 SmartScreen 拦「未知发布者」→ 点「仍要运行」。
> - macOS 开 .dmg 时 Gatekeeper 拦 → 右键 App「打开」，或终端 `xattr -cr /Applications/eos-float.app`。
> 要消除警告需 Apple / Microsoft 开发者证书（配进 secrets 即可，本仓库暂未做）。
