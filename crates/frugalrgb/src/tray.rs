//! The tray icon.
//!
//! On Linux frugalRGB speaks to the desktop's StatusNotifierItem host
//! itself, because of one detail: with a click the host hands over an
//! activation token, and on Wayland a program can only put its window in
//! front of the others with that token. Elsewhere Slint's tray icon does.

use crate::gui::{bring_up, MainWindow};

/// The icon in the desktop's tray, with its menu.
pub struct TrayIcon(imp::Inner);

impl TrayIcon {
    /// Put the icon in the tray. `rgba` is a square picture of `size`
    /// pixels. Clicks and menu entries act on `window`.
    pub fn new(window: slint::Weak<MainWindow>, rgba: Vec<u8>, size: u32) -> Result<Self, String> {
        imp::Inner::new(window, rgba, size).map(TrayIcon)
    }

    /// The presets offered under "Load Preset".
    pub fn set_presets(&self, names: Vec<String>) {
        self.0.set_presets(names);
    }
}

/// What a tray entry does, on the UI thread.
fn act(window: &MainWindow, action: Action) {
    match action {
        Action::Show => bring_up(window),
        Action::LoadPreset(name) => window.invoke_preset_selected(name.into()),
        Action::LedsOff => window.invoke_leds_off(),
        Action::Quit => window.invoke_quit_requested(),
    }
}

enum Action {
    Show,
    LoadPreset(String),
    LedsOff,
    Quit,
}

#[cfg(target_os = "linux")]
mod imp {
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    use ksni::blocking::TrayMethods;
    use ksni::menu::{MenuItem, StandardItem, SubMenu};

    use super::{act, Action};
    use crate::gui::MainWindow;

    /// The token of the latest click, until the next window uses it up.
    static ACTIVATION_TOKEN: Mutex<Option<String>> = Mutex::new(None);

    /// The token a tray click came with, for the window it opens.
    pub fn take_activation_token() -> Option<String> {
        ACTIVATION_TOKEN.lock().ok()?.take()
    }

    /// The host sends the token next to the click, sometimes a moment later.
    fn wait_for_token() -> Option<String> {
        let deadline = Instant::now() + Duration::from_millis(30);
        loop {
            if let Some(token) = ksni::current_xdg_activation_token() {
                return Some(token);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    struct Model {
        window: slint::Weak<MainWindow>,
        icon: ksni::Icon,
        presets: Vec<String>,
    }

    impl Model {
        /// Called on the tray's own thread: carry the action to the UI.
        fn send(&self, action: Action) {
            if matches!(action, Action::Show) {
                let token = wait_for_token();
                log::debug!("tray click, activation token: {}", if token.is_some() { "yes" } else { "no" });
                if let Ok(mut slot) = ACTIVATION_TOKEN.lock() {
                    *slot = token;
                }
            }
            let _ = self.window.upgrade_in_event_loop(move |window| act(&window, action));
        }

        fn entry(label: &str, action: fn() -> Action) -> MenuItem<Self> {
            StandardItem {
                label: label.into(),
                activate: Box::new(move |model: &mut Self| model.send(action())),
                ..Default::default()
            }
            .into()
        }
    }

    impl ksni::Tray for Model {
        fn id(&self) -> String {
            "frugalrgb".into()
        }

        fn title(&self) -> String {
            "frugalRGB".into()
        }

        fn tool_tip(&self) -> ksni::ToolTip {
            ksni::ToolTip { title: "frugalRGB".into(), ..Default::default() }
        }

        fn icon_pixmap(&self) -> Vec<ksni::Icon> {
            vec![self.icon.clone()]
        }

        fn activate(&mut self, _x: i32, _y: i32) {
            self.send(Action::Show);
        }

        fn menu(&self) -> Vec<MenuItem<Self>> {
            let presets = self
                .presets
                .iter()
                .map(|name| {
                    let preset = name.clone();
                    StandardItem {
                        label: name.clone(),
                        activate: Box::new(move |model: &mut Self| model.send(Action::LoadPreset(preset.clone()))),
                        ..Default::default()
                    }
                    .into()
                })
                .collect();
            vec![
                Self::entry("Show", || Action::Show),
                SubMenu { label: "Load Preset".into(), submenu: presets, ..Default::default() }.into(),
                Self::entry("LEDs Off", || Action::LedsOff),
                MenuItem::Separator,
                Self::entry("Quit", || Action::Quit),
            ]
        }
    }

    pub struct Inner(ksni::blocking::Handle<Model>);

    impl Inner {
        pub fn new(window: slint::Weak<MainWindow>, rgba: Vec<u8>, size: u32) -> Result<Self, String> {
            // The protocol wants ARGB, highest byte first
            let data = rgba.chunks_exact(4).flat_map(|p| [p[3], p[0], p[1], p[2]]).collect();
            let icon = ksni::Icon { width: size as i32, height: size as i32, data };
            Model { window, icon, presets: Vec::new() }.spawn().map(Inner).map_err(|e| e.to_string())
        }

        pub fn set_presets(&self, names: Vec<String>) {
            self.0.update(|model| model.presets = names);
        }
    }

    impl Drop for Inner {
        fn drop(&mut self) {
            self.0.shutdown();
        }
    }
}

#[cfg(target_os = "linux")]
pub use imp::take_activation_token;

#[cfg(not(target_os = "linux"))]
mod imp {
    use slint::{ModelRc, Rgba8Pixel, SharedPixelBuffer, SharedString, VecModel};

    use super::{act, Action};
    use crate::gui::{MainWindow, Tray};

    pub struct Inner(Tray);

    impl Inner {
        pub fn new(window: slint::Weak<MainWindow>, rgba: Vec<u8>, size: u32) -> Result<Self, String> {
            let tray = Tray::new().map_err(|e| e.to_string())?;
            tray.set_tray_icon(slint::Image::from_rgba8(SharedPixelBuffer::<Rgba8Pixel>::clone_from_slice(
                &rgba, size, size,
            )));

            let on = |action: fn() -> Action| {
                let window = window.clone();
                move || {
                    if let Some(window) = window.upgrade() {
                        act(&window, action());
                    }
                }
            };
            tray.on_show_window(on(|| Action::Show));
            tray.on_leds_off(on(|| Action::LedsOff));
            tray.on_quit(on(|| Action::Quit));
            let target = window.clone();
            tray.on_load_preset(move |name| {
                if let Some(window) = target.upgrade() {
                    act(&window, Action::LoadPreset(name.to_string()));
                }
            });
            Ok(Inner(tray))
        }

        pub fn set_presets(&self, names: Vec<String>) {
            let names: Vec<SharedString> = names.into_iter().map(Into::into).collect();
            self.0.set_presets(ModelRc::new(VecModel::from(names)));
        }
    }
}
