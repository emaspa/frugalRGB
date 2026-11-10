//! The window and the tray icon.
//!
//! The UI thread never touches hardware: it describes what the zones should
//! look like and hands that to the device worker.

use std::cell::RefCell;
use std::fs::File;
use std::rc::Rc;
use std::time::Duration;

use frugalrgb_core::engine::{self, Card};
use frugalrgb_core::store::{self, Calibration, Config, Preset, Presets, ZoneState, NO_PRESET};
use frugalrgb_core::transport::SharedBus;
use frugalrgb_core::worker::{Command, Event, Worker};
use frugalrgb_core::{detect, diagnostics, Correction, DeviceInfo, Mode, Rgb};

use crate::{autostart, logging};
use slint::{Color, ComponentHandle, Model, ModelRc, Rgba8Pixel, SharedPixelBuffer, SharedString, VecModel};

slint::include_modules!();

/// Effect names as shown, in menu order.
const EFFECTS: [(&str, Mode); 6] = [
    ("Static", Mode::Static),
    ("Breathing", Mode::Breathing),
    ("Color Cycle", Mode::ColorCycle),
    ("Rainbow", Mode::Rainbow),
    ("Strobe", Mode::Strobe),
    ("Off", Mode::Off),
];

fn rgb(color: Color) -> Rgb {
    [color.red(), color.green(), color.blue()]
}

fn color([r, g, b]: Rgb) -> Color {
    Color::from_rgb_u8(r, g, b)
}

fn strings(items: impl IntoIterator<Item = impl Into<SharedString>>) -> ModelRc<SharedString> {
    ModelRc::new(VecModel::from(items.into_iter().map(Into::into).collect::<Vec<_>>()))
}

/// `#RRGGBB`, `RRGGBB` or `R,G,B`.
fn parse_color(text: &str) -> Option<Rgb> {
    let text = text.trim();
    let hex = text.strip_prefix('#').unwrap_or(text);
    if hex.len() == 6 && hex.chars().all(|c| c.is_ascii_hexdigit()) {
        let channel = |i| u8::from_str_radix(&hex[i..i + 2], 16).ok();
        return Some([channel(0)?, channel(2)?, channel(4)?]);
    }
    let parts: Vec<u8> = text
        .split([',', ' '])
        .filter(|p| !p.is_empty())
        .map(|p| p.parse::<u16>().map(|v| v.min(255) as u8))
        .collect::<Result<_, _>>()
        .ok()?;
    <[u8; 3]>::try_from(parts).ok()
}

/// The app icon: red, green and blue discs that mix where they overlap, on
/// a transparent background.
fn app_icon(size: u32) -> slint::Image {
    let s = size as f32;
    let (cx, cy) = (s / 2.0, s / 2.0);
    let radius = s * 0.32;
    let spread = s * 0.16;
    let centers = [
        (cx, cy - spread),
        (cx - spread * 0.87, cy + spread * 0.5),
        (cx + spread * 0.87, cy + spread * 0.5),
    ];
    let glow = s / 10.0;

    let mut buffer = SharedPixelBuffer::<Rgba8Pixel>::new(size, size);
    for (i, pixel) in buffer.make_mut_slice().iter_mut().enumerate() {
        let (x, y) = ((i as u32 % size) as f32 + 0.5, (i as u32 / size) as f32 + 0.5);
        let channel = |(ox, oy): (f32, f32)| {
            let distance = ((x - ox).powi(2) + (y - oy).powi(2)).sqrt();
            let disc = (radius - distance + 0.5).clamp(0.0, 1.0);
            let halo = (1.0 - (distance - radius) / glow).clamp(0.0, 1.0).powi(2);
            ((0.7 * disc + 0.3 * halo.max(disc)) * 255.0) as u8
        };
        let (r, g, b) = (channel(centers[0]), channel(centers[1]), channel(centers[2]));
        // Black is where nothing is drawn: make it transparent
        let alpha = (0.299 * f32::from(r) + 0.587 * f32::from(g) + 0.114 * f32::from(b)) as u8;
        *pixel = Rgba8Pixel { r, g, b, a: alpha.max(r.max(g).max(b) / 2) };
    }
    slint::Image::from_rgba8(buffer)
}

/// Only one frugalRGB may drive the devices. `None` means another one holds
/// the lock. The lock file is the one the Python versions used, so they are
/// kept out as well.
fn single_instance_lock() -> Result<Option<File>, String> {
    let path = std::env::temp_dir().join("frugalrgb.lock");
    let file = File::options()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(file.try_lock().is_ok().then_some(file))
}

/// Show a message in a small window of its own and wait for the user.
fn notice(message: &str) -> Result<(), String> {
    let fail = |e: slint::PlatformError| e.to_string();
    let window = NoticeWindow::new().map_err(fail)?;
    window.set_app_icon(app_icon(64));
    window.set_message(message.into());
    let weak = window.as_weak();
    window.on_dismissed(move || {
        if let Some(window) = weak.upgrade() {
            let _ = window.hide();
        }
    });
    window.run().map_err(fail)
}

struct App {
    window: slint::Weak<MainWindow>,
    tray: Option<Tray>,
    devices: Vec<DeviceInfo>,
    /// What each device says about itself (firmware, address, ...).
    facts: Vec<Vec<(&'static str, String)>>,
    /// The memory's bus, which a diagnostics report scans.
    smbus: Option<SharedBus>,
    /// The device and zone behind each row of the zone list.
    card_zones: Vec<(usize, u8)>,
    cards: Rc<VecModel<ZoneCard>>,
    config: Config,
    presets: Presets,
    worker: Option<Worker>,
}

impl App {
    fn window(&self) -> MainWindow {
        self.window.upgrade().expect("the window outlives its callbacks")
    }

    fn send(&self, command: Command) {
        if let Some(worker) = &self.worker {
            worker.send(command);
        }
    }

    fn status(&self, text: impl Into<SharedString>) {
        self.window().set_status(text.into());
    }

    fn mode(&self) -> Mode {
        let index = usize::try_from(self.window().get_effect_index()).unwrap_or(0);
        EFFECTS.get(index).map_or(Mode::Static, |(_, mode)| *mode)
    }

    fn brush(&self) -> Rgb {
        rgb(self.window().get_brush())
    }

    fn show_brush(&self) {
        let [r, g, b] = self.brush();
        self.window().set_brush_text(format!("#{r:02X}{g:02X}{b:02X}   {r}, {g}, {b}").into());
    }

    fn set_brush(&self, shade: Rgb) {
        self.window().invoke_set_brush(color(shade));
        self.show_brush();
    }

    fn engine_cards(&self) -> Vec<Card> {
        self.cards
            .iter()
            .zip(&self.card_zones)
            .map(|(row, &(device, zone))| Card { device, zone, enabled: row.enabled, color: rgb(row.color) })
            .collect()
    }

    /// Write what the cards show to the devices.
    fn apply(&self) {
        let mode = self.mode();
        if mode == Mode::Off {
            return self.send(Command::Off);
        }
        let plans = engine::resolve(&self.engine_cards(), &self.devices);
        log::info!("Applying: effect={} devices={}", mode.effect_name(), plans.len());
        self.send(Command::Apply { mode, speed: f64::from(self.window().get_speed()), plans });
    }

    /// The user moved the brush: every checked zone takes its colour.
    fn paint_checked(&self) {
        let shade = self.window().get_brush();
        for index in 0..self.cards.row_count() {
            let Some(mut row) = self.cards.row_data(index) else { continue };
            if row.enabled && row.color != shade {
                row.color = shade;
                self.cards.set_row_data(index, row);
            }
        }
        self.show_brush();
    }

    fn update_card(&self, index: i32, change: impl FnOnce(&mut ZoneCard)) {
        let Ok(index) = usize::try_from(index) else { return };
        if let Some(mut row) = self.cards.row_data(index) {
            change(&mut row);
            self.cards.set_row_data(index, row);
        }
    }

    // -- Calibration --

    fn calibration_device(&self) -> Option<&DeviceInfo> {
        self.devices.get(usize::try_from(self.window().get_calibration_device()).ok()?)
    }

    /// Show the selected device's calibration on the sliders.
    fn show_calibration(&self) {
        let Some(device) = self.calibration_device() else { return };
        let Correction([r, g, b]) = self.config.calibration.for_device(&device.name);
        let window = self.window();
        window.set_cal_r(r as f32);
        window.set_cal_g(g as f32);
        window.set_cal_b(b as f32);
    }

    fn calibration_changed(&mut self) {
        let window = self.window();
        let Ok(index) = usize::try_from(window.get_calibration_device()) else { return };
        let Some(name) = self.devices.get(index).map(|d| d.name.clone()) else { return };
        let triple = [window.get_cal_r(), window.get_cal_g(), window.get_cal_b()].map(f64::from);

        // An old config holds one triple for all devices: give each its own
        if let Calibration::Global(shared) = self.config.calibration {
            self.config.calibration =
                Calibration::PerDevice(self.devices.iter().map(|d| (d.name.clone(), shared)).collect());
        }
        if let Calibration::PerDevice(map) = &mut self.config.calibration {
            map.insert(name, triple);
        }
        self.send(Command::Calibrate { device: index, correction: Correction(triple) });
        self.apply();
    }

    // -- Presets --

    fn refresh_presets(&self) {
        let window = self.window();
        let names: Vec<&str> = std::iter::once(NO_PRESET).chain(self.presets.keys().map(String::as_str)).collect();
        let startup = self.config.startup_preset().and_then(|name| self.presets.get_index_of(name));
        window.set_presets(strings(names.iter().copied()));
        window.set_startup_index(startup.map_or(0, |i| i as i32 + 1));
        if let Some(tray) = &self.tray {
            tray.set_presets(strings(self.presets.keys().map(String::as_str)));
        }
    }

    fn select_preset(&self, name: &str) {
        let Some((index, _, preset)) = self.presets.get_full(name) else { return };
        let window = self.window();

        let mode = preset.mode();
        window.set_effect_index(EFFECTS.iter().position(|(_, m)| *m == mode).unwrap_or(0) as i32);
        window.set_speed(preset.speed() as f32);

        for (row, card) in preset.cards(&self.devices).into_iter().enumerate() {
            if let Some(mut data) = self.cards.row_data(row) {
                data.enabled = card.enabled;
                data.color = color(card.color);
                self.cards.set_row_data(row, data);
            }
        }
        self.set_brush(preset.color);
        window.set_preset_index(index as i32 + 1);
        log::info!("Loaded preset: {name}");
        self.apply();
    }

    fn selected_preset(&self) -> Option<String> {
        let index = usize::try_from(self.window().get_preset_index()).ok()?.checked_sub(1)?;
        self.presets.get_index(index).map(|(name, _)| name.clone())
    }

    /// Save the current state under `name`, or over the selected preset
    /// when no name is given.
    fn save_preset(&mut self, name: &str) {
        let name = match name.trim() {
            "" => self.selected_preset(),
            NO_PRESET => None,
            typed => Some(typed.to_string()),
        };
        let Some(name) = name else {
            return self.status("Type a name for the new preset first");
        };

        let window = self.window();
        let devices = self
            .cards
            .iter()
            .zip(&self.card_zones)
            .filter_map(|(row, &(device, zone))| {
                let info = self.devices.get(device)?;
                let zone = info.zones.iter().find(|z| z.id == zone)?;
                Some((
                    store::zone_key(info, zone),
                    ZoneState { enabled: row.enabled, color: Some(rgb(row.color)) },
                ))
            })
            .collect();
        let preset = Preset {
            color: self.brush(),
            effect: Some(self.mode().effect_name().to_string()),
            speed: Some(f64::from(window.get_speed())),
            devices,
        };
        self.presets.insert(name.clone(), preset);

        match store::save_presets(&store::presets_path(), &self.presets) {
            Ok(()) => self.status(format!("Saved preset \"{name}\"")),
            Err(e) => self.status(format!("Could not save the preset: {e}")),
        }
        self.refresh_presets();
        window.set_preset_index(self.presets.get_index_of(&name).map_or(0, |i| i as i32 + 1));
        window.set_preset_name(SharedString::new());
    }

    fn delete_preset(&mut self) {
        let Some(name) = self.selected_preset() else { return };
        self.presets.shift_remove(&name);
        match store::save_presets(&store::presets_path(), &self.presets) {
            Ok(()) => self.status(format!("Deleted preset \"{name}\"")),
            Err(e) => self.status(format!("Could not save the presets: {e}")),
        }
        if self.config.startup_preset() == Some(name.as_str()) {
            self.config.startup_preset = NO_PRESET.to_string();
            self.save_config();
        }
        self.window().set_preset_index(0);
        self.refresh_presets();
    }

    // -- Settings --

    fn save_config(&self) {
        if let Err(e) = store::save_config(&store::config_path(), &self.config) {
            log::error!("Failed to save config: {e}");
        }
    }

    fn options_changed(&mut self) {
        let window = self.window();
        self.config.off_on_close = window.get_off_on_close();
        self.config.minimize_to_tray = window.get_close_to_tray();
        self.config.start_minimized = window.get_start_minimized();
        self.save_config();
    }

    fn save_to_hardware(&self) {
        let devices: Vec<usize> =
            self.devices.iter().enumerate().filter(|(_, d)| d.supports_save).map(|(i, _)| i).collect();
        self.status("Saving to hardware...");
        self.send(Command::Save { devices });
    }

    /// Write a diagnostics report next to the user's other files. The
    /// device thread collects it, as it owns the devices.
    fn diagnostics(&self) {
        let window = self.window();
        window.set_diagnostics_running(true);
        self.status("Collecting diagnostics...");

        let smbus = self.smbus.clone();
        let weak = self.window.clone();
        self.send(Command::Run(Box::new(move |controllers| {
            let report = diagnostics::report(controllers, smbus.as_ref(), &logging::recent());
            let outcome = match diagnostics::save(&report) {
                Ok(path) => {
                    format!("Diagnostics saved to:\n{}\n\nAttach this file to your bug report.", path.display())
                }
                Err(e) => format!("Could not save the diagnostics: {e}"),
            };
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_diagnostics_running(false);
                window.set_status(SharedString::new());
                window.set_notice(outcome.into());
            });
        })));
    }

    /// Show what is known about the device picked in the test dialog.
    fn show_test_device(&self) {
        let window = self.window();
        let index = usize::try_from(window.get_test_device()).unwrap_or(0);
        let mut lines: Vec<String> = Vec::new();
        if let Some(device) = self.devices.get(index) {
            lines.push(format!("Zones: {}", device.zones.len()));
        }
        if let Some(facts) = self.facts.get(index) {
            lines.extend(facts.iter().map(|(label, value)| format!("{label}: {value}")));
        }
        window.set_test_facts(lines.join("\n").into());
        window.set_test_status(SharedString::new());
    }

    /// Run the colour test on the picked device, then put the cards back.
    fn run_test(&self) {
        let window = self.window();
        let Ok(index) = usize::try_from(window.get_test_device()) else { return };
        window.set_test_running(true);

        let weak = self.window.clone();
        self.send(Command::Run(Box::new(move |controllers| {
            let result = match controllers.get_mut(index) {
                Some(ctrl) => engine::test_colors(ctrl.as_mut(), Duration::from_millis(1300), |shade| {
                    let text = format!("Showing: {shade}");
                    let _ = weak.upgrade_in_event_loop(move |window| window.set_test_status(text.into()));
                }),
                None => Ok(()),
            };
            let done = match result {
                Ok(()) => "Done. Your colors are back.".to_string(),
                Err(e) => format!("The device did not take the test: {e}"),
            };
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_test_status(done.into());
                window.set_test_running(false);
                window.invoke_apply();
            });
        })));
    }

    /// Remember the window's size while it is on screen.
    fn remember_size(&mut self) {
        let window = self.window();
        if window.window().is_visible() {
            let size = window.window().size().to_logical(window.window().scale_factor());
            self.config.set_window_size(size.width as u32, size.height as u32);
        }
    }

    /// Leave the program: settings are saved and the devices get their last
    /// write (lights off, if the user asked for that on close).
    fn quit(&mut self) {
        self.remember_size();
        self.save_config();
        if let Some(worker) = self.worker.take() {
            worker.shutdown(self.config.off_on_close);
        }
        // Dropping the tray removes the icon and lets the event loop end
        self.tray = None;
        let _ = slint::quit_event_loop();
    }
}

fn describe(event: Event) -> String {
    let (done, failures) = match event {
        Event::Applied { failures } => ("Applied", failures),
        Event::Saved { failures } => ("Settings saved to hardware flash", failures),
    };
    if failures.is_empty() {
        done.to_string()
    } else {
        failures.join("; ")
    }
}

/// Open the window (or just the tray icon) and run until the user quits.
pub fn run() -> Result<(), String> {
    let Some(_lock) = single_instance_lock()? else {
        return notice("frugalRGB is already running. Look for its icon in the system tray.");
    };
    let fail = |e: slint::PlatformError| e.to_string();

    let config = store::load_config(&store::config_path()).unwrap_or_else(|e| {
        log::error!("{e}");
        Config::default()
    });
    let presets = store::load_presets(&store::presets_path()).unwrap_or_else(|e| {
        log::error!("{e}");
        Presets::new()
    });

    let detect::Hardware { mut controllers, smbus } = detect::scan();
    config.calibrate(&mut controllers);
    let devices = DeviceInfo::all(&controllers);
    let facts = controllers.iter().map(|ctrl| ctrl.facts()).collect();
    log::info!("Found {} RGB device(s)", devices.len());

    let window = MainWindow::new().map_err(fail)?;
    window.set_app_icon(app_icon(256));
    window.set_version(concat!("v", env!("CARGO_PKG_VERSION")).into());
    window.set_effects(strings(EFFECTS.iter().map(|(name, _)| *name)));
    window.set_devices(strings(devices.iter().map(|d| d.name.as_str())));
    window.set_no_devices_hint(
        if cfg!(windows) {
            "No RGB devices detected.\n\nClose other RGB software (Armoury Crate, RGB Fusion, Mystic Light) and start frugalRGB again. Memory RGB needs PawnIO and Run as administrator."
        } else {
            "No RGB devices detected.\n\nInstall the udev rules (70-frugalrgb.rules) for USB devices and join the 'i2c' group for memory RGB."
        }
        .into(),
    );
    window.set_can_save_hardware(devices.iter().any(|d| d.supports_save));
    window.set_save_targets(
        devices.iter().filter(|d| d.supports_save).map(|d| format!("  - {}", d.name)).collect::<Vec<_>>().join("\n").into(),
    );
    window.set_off_on_close(config.off_on_close);
    window.set_close_to_tray(config.minimize_to_tray);
    window.set_start_minimized(config.start_minimized);
    window.set_autostart_supported(autostart::supported());
    window.set_start_at_login(autostart::enabled());
    if let Some((width, height)) = config.window_size() {
        window.set_start_width(width as f32);
        window.set_start_height(height as f32);
    }

    // One row per zone, device by device
    let mut card_zones = Vec::new();
    let mut rows = Vec::new();
    for (index, device) in devices.iter().enumerate() {
        for (position, zone) in device.zones.iter().enumerate() {
            card_zones.push((index, zone.id));
            rows.push(ZoneCard {
                device: device.name.as_str().into(),
                zone: zone.name.as_str().into(),
                header: position == 0,
                enabled: true,
                color: Color::from_rgb_u8(255, 255, 255),
            });
        }
    }
    let cards = Rc::new(VecModel::from(rows));
    window.set_cards(ModelRc::from(Rc::clone(&cards)));

    // Device results come back on the worker's thread
    let status_target = window.as_weak();
    let worker = Worker::spawn(controllers, move |event| {
        let text = describe(event);
        let _ = status_target.upgrade_in_event_loop(move |window| window.set_status(text.into()));
    });

    let tray = match Tray::new() {
        Ok(tray) => {
            tray.set_tray_icon(app_icon(64));
            Some(tray)
        }
        Err(e) => {
            log::warn!("No tray icon: {e}");
            None
        }
    };
    window.set_tray_available(tray.is_some());

    let app = Rc::new(RefCell::new(App {
        window: window.as_weak(),
        tray,
        devices,
        facts,
        smbus,
        card_zones,
        cards,
        config,
        presets,
        worker: Some(worker),
    }));

    // -- Window callbacks --
    // Each handler borrows the app for the length of one UI event.
    macro_rules! on {
        ($setter:ident, |$app:ident $(, $arg:ident)*| $body:expr) => {{
            let shared = Rc::clone(&app);
            window.$setter(move |$($arg),*| {
                #[allow(unused_mut)]
                let mut $app = shared.borrow_mut();
                $body
            });
        }};
    }

    on!(on_card_toggled, |app, index, enabled| app.update_card(index, |row| row.enabled = enabled));
    on!(on_card_painted, |app, index| {
        let shade = app.window().get_brush();
        app.update_card(index, |row| {
            row.color = shade;
            row.enabled = true;
        });
    });
    on!(on_brush_picked, |app| app.paint_checked());
    on!(on_hex_entered, |app, text| match parse_color(&text) {
        Some(shade) => {
            app.set_brush(shade);
            app.paint_checked();
        }
        None => app.status("Colors look like #FF8000 or 255,128,0"),
    });
    on!(on_effect_changed, |app| app.apply());
    on!(on_apply, |app| app.apply());
    on!(on_leds_off, |app| app.send(Command::Off));
    on!(on_save_to_hardware, |app| app.save_to_hardware());

    on!(on_calibration_device_changed, |app| app.show_calibration());
    on!(on_calibration_changed, |app| app.calibration_changed());
    on!(on_calibration_released, |app| app.save_config());

    on!(on_preset_selected, |app, name| app.select_preset(&name));
    on!(on_save_preset, |app, name| app.save_preset(&name));
    on!(on_delete_preset, |app| app.delete_preset());
    on!(on_startup_preset_changed, |app, name| {
        app.config.startup_preset = name.to_string();
        app.save_config();
    });

    on!(on_options_changed, |app| app.options_changed());
    on!(on_diagnostics, |app| app.diagnostics());
    on!(on_test_device_changed, |app| app.show_test_device());
    on!(on_run_test, |app| app.run_test());
    on!(on_start_at_login_toggled, |app, enabled| {
        if let Err(e) = autostart::set(enabled) {
            app.status(format!("Could not change the autostart entry: {e}"));
            app.window().set_start_at_login(!enabled);
        }
    });

    // Closing the window hides it when a tray icon can bring it back
    {
        let shared = Rc::clone(&app);
        window.window().on_close_requested(move || {
            let mut app = shared.borrow_mut();
            app.remember_size();
            if !(app.config.minimize_to_tray && app.tray.is_some()) {
                app.quit();
            }
            slint::CloseRequestResponse::HideWindow
        });
    }

    // -- Tray callbacks --
    if let Some(tray) = &app.borrow().tray {
        let weak = window.as_weak();
        tray.on_show_window(move || {
            if let Some(window) = weak.upgrade() {
                if let Err(e) = window.show() {
                    log::error!("Could not show the window: {e}");
                }
                // A window that starts hidden already exists on Wayland and
                // has used up the one redraw it got when it was created.
                // Without a frame the compositor never puts it on screen.
                window.window().request_redraw();
            }
        });
        let shared = Rc::clone(&app);
        tray.on_load_preset(move |name| shared.borrow().select_preset(&name));
        let shared = Rc::clone(&app);
        tray.on_leds_off(move || shared.borrow().send(Command::Off));
        let shared = Rc::clone(&app);
        tray.on_quit(move || shared.borrow_mut().quit());
    }

    // -- Initial state --
    let start_hidden = {
        let app = app.borrow();
        app.refresh_presets();
        app.show_calibration();
        app.set_brush([255, 255, 255]);
        if let Some(name) = app.config.startup_preset().map(str::to_string) {
            app.select_preset(&name);
        }
        app.config.start_minimized && app.tray.is_some()
    };

    #[cfg(debug_assertions)]
    let _snapshot_timer = snapshot_for_development(&window, &app);

    if !start_hidden {
        window.show().map_err(fail)?;
    }
    slint::run_event_loop().map_err(fail)?;

    // The loop can also end without quit(), e.g. the session going away
    app.borrow_mut().quit();
    Ok(())
}

/// Development aid: with `FRUGALRGB_SNAPSHOT=file.ppm` set, a debug build
/// writes a picture of its window to that file and exits. Before that it
/// clicks the positions in `FRUGALRGB_CLICKS="x,y;x,y"` (logical pixels),
/// which makes the wiring between the window and the app checkable.
#[cfg(debug_assertions)]
fn snapshot_for_development(window: &MainWindow, app: &Rc<RefCell<App>>) -> Option<slint::Timer> {
    use slint::platform::{PointerEventButton, WindowEvent};

    let path = std::env::var_os("FRUGALRGB_SNAPSHOT")?;
    let mut clicks: std::collections::VecDeque<(f32, f32)> = std::env::var("FRUGALRGB_CLICKS")
        .unwrap_or_default()
        .split(';')
        .filter_map(|pair| {
            let (x, y) = pair.split_once(',')?;
            Some((x.trim().parse().ok()?, y.trim().parse().ok()?))
        })
        .collect();

    let weak = window.as_weak();
    let shared = Rc::clone(app);
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, std::time::Duration::from_millis(400), move || {
        let Some(window) = weak.upgrade() else { return };
        if let Some((x, y)) = clicks.pop_front() {
            let position = slint::LogicalPosition::new(x, y);
            let button = PointerEventButton::Left;
            window.window().dispatch_event(WindowEvent::PointerMoved { position });
            window.window().dispatch_event(WindowEvent::PointerPressed { position, button });
            window.window().dispatch_event(WindowEvent::PointerReleased { position, button });
            return;
        }
        match window.window().take_snapshot() {
            Ok(image) => {
                let mut ppm = format!("P6\n{} {}\n255\n", image.width(), image.height()).into_bytes();
                ppm.extend(image.as_slice().iter().flat_map(|p| [p.r, p.g, p.b]));
                if let Err(e) = std::fs::write(&path, ppm) {
                    log::error!("snapshot: {e}");
                }
            }
            Err(e) => log::error!("snapshot: {e}"),
        }
        shared.borrow_mut().quit();
    });
    Some(timer)
}
