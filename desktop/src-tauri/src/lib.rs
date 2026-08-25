mod backend;
mod updates;

use std::sync::Arc;

use backend::{start_backend, BackendState};
use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

const DESKTOP_PORT: u16 = 18000;

fn allowed_navigation(url: &tauri::Url) -> bool {
    if url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost") {
        return true;
    }
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == Some(DESKTOP_PORT)
}

fn is_external_navigation(url: &tauri::Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
}

fn install_menu(app: &tauri::App) -> tauri::Result<()> {
    let application = SubmenuBuilder::new(app, "llms.py")
        .about(None)
        .separator()
        .text("check-updates", "Check for Updates…")
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .separator()
        .quit()
        .build()?;
    let file = SubmenuBuilder::new(app, "File")
        .text("open-log", "Open Desktop Log")
        .separator()
        .close_window()
        .build()?;
    let edit = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;
    let view = SubmenuBuilder::new(app, "View")
        .text("reload", "Reload")
        .separator()
        .fullscreen()
        .build()?;
    let window = SubmenuBuilder::new(app, "Window")
        .minimize()
        .maximize()
        .build()?;
    let menu = MenuBuilder::new(app)
        .items(&[&application, &file, &edit, &view, &window])
        .build()?;
    app.set_menu(menu)?;

    app.on_menu_event(|app, event| match event.id().as_ref() {
        "open-log" => {
            if let Ok(path) = backend::desktop_log_path(app) {
                let _ = app.opener().open_path(path.to_string_lossy(), None::<&str>);
            }
        }
        "reload" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("window.location.reload()");
            }
        }
        "check-updates" => updates::check_for_updates(app.clone()),
        _ => {}
    });
    Ok(())
}

pub fn run() {
    let backend = Arc::new(BackendState::default());
    let managed_backend = Arc::clone(&backend);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(managed_backend)
        .setup(|app| {
            install_menu(app)?;
            let opener_app = app.handle().clone();
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("llms.py")
                    .inner_size(1280.0, 820.0)
                    .min_inner_size(720.0, 560.0)
                    .center()
                    .on_navigation(move |url| {
                        if allowed_navigation(url) {
                            return true;
                        }
                        if is_external_navigation(url) {
                            let _ = opener_app.opener().open_url(url.as_str(), None::<&str>);
                        }
                        false
                    })
                    .build()?;
            window.show()?;

            let state = app.state::<Arc<BackendState>>();
            if let Err(error) = start_backend(&app.handle().clone(), Arc::clone(state.inner())) {
                backend::show_error(&app.handle().clone(), &error.to_string());
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build llms.py desktop application");

    app.run(move |_app_handle, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            backend.shutdown();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn navigation_is_limited_to_packaged_ui_and_fixed_desktop_origin() {
        assert!(allowed_navigation(
            &"tauri://localhost/index.html".parse().unwrap()
        ));
        assert!(allowed_navigation(
            &"http://127.0.0.1:18000/".parse().unwrap()
        ));
        assert!(!allowed_navigation(
            &"http://127.0.0.1:18000@evil.example/".parse().unwrap()
        ));
        assert!(!allowed_navigation(
            &"http://127.0.0.1:8000/".parse().unwrap()
        ));
        assert!(!allowed_navigation(
            &"https://example.com/".parse().unwrap()
        ));
    }

    #[test]
    fn only_non_loopback_web_links_are_external() {
        assert!(is_external_navigation(
            &"https://example.com/".parse().unwrap()
        ));
        assert!(!is_external_navigation(
            &"http://127.0.0.1:8000/".parse().unwrap()
        ));
        assert!(!is_external_navigation(
            &"tauri://localhost/index.html".parse().unwrap()
        ));
    }
}
