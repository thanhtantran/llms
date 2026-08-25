use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

pub fn check_for_updates(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let result = match app.updater() {
            Ok(updater) => updater.check().await,
            Err(error) => {
                show_error(&app, format!("The updater is not configured: {error}"));
                return;
            }
        };

        match result {
            Ok(Some(update)) => {
                let version = update.version.clone();
                let update_app = app.clone();
                app.dialog()
                    .message(format!(
                        "llms.py Desktop {version} is available. Download and install it now?"
                    ))
                    .title("Update available")
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        "Install Update".into(),
                        "Later".into(),
                    ))
                    .show(move |install| {
                        if install {
                            install_update(update_app, update, version);
                        }
                    });
            }
            Ok(None) => {
                app.dialog()
                    .message("You are running the latest version of llms.py Desktop.")
                    .title("No updates available")
                    .show(|_| {});
            }
            Err(error) => show_error(&app, format!("Could not check for updates: {error}")),
        }
    });
}

fn install_update(app: AppHandle, update: tauri_plugin_updater::Update, version: String) {
    tauri::async_runtime::spawn(async move {
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => {
                app.dialog()
                    .message(format!(
                        "llms.py Desktop {version} is installed. Quit and reopen the app to use it."
                    ))
                    .title("Update installed")
                    .show(|_| {});
            }
            Err(error) => show_error(&app, format!("Could not install the update: {error}")),
        }
    });
}

fn show_error(app: &AppHandle, message: String) {
    app.dialog()
        .message(message)
        .kind(MessageDialogKind::Error)
        .title("llms.py Desktop")
        .show(|_| {});
}
