#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! English OS · 摸鱼模式（设计方案 §11）
//!
//! 一个极薄的原生壳：**透明 + 无边框 + 常驻顶端**，里面装的就是
//! 已部署在 Render 上的 stealth.html（练习 + AI 批改 + 朗读全复用）。
//!
//! 与 English OS 本体零耦合：
//!   - 后端连远程 Render（地址写在 tauri.conf.json 的 app.windows[0].url）
//!   - 删掉 desktop/ 目录即回退，不影响本体任何功能
//!
//! 窗口三要素（§11.4，均在 tauri.conf.json 里配好）：
//!   transparent(true)   —— 真透明，透出 OS 桌面 / 下面的软件（不是毛玻璃）
//!   decorations(false)  —— 无边框无标题栏；stealth.html 的 .st-bar 带
//!                          data-tauri-drag-region，按住标题栏即可拖动
//!   always_on_top(true) —— 压在 Excel / 视频 / 任何软件上方

use tauri::Manager;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // 主窗口已在 tauri.conf.json 里声明（外部 URL + 透明/无边框/置顶）。
            // 这里只做一件事：开局贴在屏幕右上角，像一条便签，不挡正事。
            if let Some(win) = app.get_webview_window("stealth") {
                if let Ok(monitor) = win.current_monitor() {
                    if let Some(m) = monitor {
                        let w = win.outer_size().unwrap_or(tauri::PhysicalSize::new(380, 640));
                        let x = m.size().width.saturating_sub(w.width).saturating_sub(24);
                        let _ = win.set_position(tauri::PhysicalPosition::new(x as f64, 80.0));
                    }
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("eos-float 启动失败");
}
