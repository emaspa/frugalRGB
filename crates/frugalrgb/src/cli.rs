//! The command-line commands: everything that needs no window.

use std::sync::atomic::AtomicBool;
#[cfg(unix)]
use std::sync::atomic::Ordering;

use std::time::Duration;

use frugalrgb_core::{detect, diagnostics, engine, store, Controller, DeviceInfo};

static STOP: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
fn stop_on_signal() {
    extern "C" fn on_signal(_: libc::c_int) {
        STOP.store(true, Ordering::Relaxed);
    }
    let handler = on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
    // SAFETY: the handler only stores to an atomic, which is signal-safe
    unsafe {
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGTERM, handler);
    }
}

#[cfg(not(unix))]
fn stop_on_signal() {}

fn devices() -> Result<Vec<Box<dyn Controller>>, String> {
    let controllers = detect::detect_all();
    if controllers.is_empty() {
        return Err("no RGB devices detected".into());
    }
    Ok(controllers)
}

pub fn list() -> Result<(), String> {
    for ctrl in devices()? {
        println!("{}", ctrl.name());
        for zone in ctrl.zones() {
            println!("  {}", zone.name);
        }
    }
    Ok(())
}

/// Print the diagnostics report: what a bug report should come with.
pub fn diagnostics() -> Result<(), String> {
    let mut hardware = detect::scan();
    let report = diagnostics::report(&mut hardware.controllers, hardware.smbus.as_ref(), &crate::logging::recent());
    print!("{report}");
    Ok(())
}

/// Show red, green, blue and white on every device in turn.
pub fn test() -> Result<(), String> {
    let mut failures = Vec::new();
    for ctrl in &mut devices()? {
        let name = ctrl.name().to_string();
        println!("{name}");
        let shown = engine::test_colors(ctrl.as_mut(), Duration::from_millis(1300), |shade| println!("  {shade}"));
        if let Err(e) = shown {
            failures.push(format!("{name}: {e}"));
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("\n"))
    }
}

pub fn presets() -> Result<(), String> {
    let presets = store::load_presets(&store::presets_path()).map_err(|e| e.to_string())?;
    let config = store::load_config(&store::config_path()).map_err(|e| e.to_string())?;
    for (name, preset) in &presets {
        let startup = if config.startup_preset() == Some(name.as_str()) { "  (on start)" } else { "" };
        println!("{name}  [{}]{startup}", preset.mode().effect_name());
    }
    Ok(())
}

pub fn apply(name: Option<&str>) -> Result<(), String> {
    let config = store::load_config(&store::config_path()).map_err(|e| e.to_string())?;
    let presets = store::load_presets(&store::presets_path()).map_err(|e| e.to_string())?;

    let name = name
        .or(config.startup_preset())
        .ok_or("no preset given and no startup preset is set")?;
    let preset = presets.get(name).ok_or_else(|| format!("no preset named \"{name}\""))?;

    let mut controllers = devices()?;
    config.calibrate(&mut controllers);

    let mode = preset.mode();
    if mode == frugalrgb_core::Mode::Off {
        return report(engine::turn_off(&mut controllers), &controllers);
    }

    let plans = {
        let infos = DeviceInfo::all(&controllers);
        engine::resolve(&preset.cards(&infos), &infos)
    };
    let failures = engine::apply(&mut controllers, mode, preset.speed(), &plans);
    report(failures, &controllers)?;

    // Devices without this effect in hardware are animated from here, for
    // as long as the process runs
    let software = engine::software_plans(&controllers, mode, &plans);
    if !software.is_empty() {
        stop_on_signal();
        engine::run_software(&mut controllers, mode, preset.speed(), &software, &STOP);
    }
    Ok(())
}

pub fn off() -> Result<(), String> {
    let mut controllers = devices()?;
    report(engine::turn_off(&mut controllers), &controllers)
}

fn report(
    failures: Vec<(usize, frugalrgb_core::Error)>,
    controllers: &[Box<dyn Controller>],
) -> Result<(), String> {
    if failures.is_empty() {
        return Ok(());
    }
    let lines: Vec<String> = failures
        .iter()
        .map(|(device, e)| format!("{}: {e}", controllers[*device].name()))
        .collect();
    Err(lines.join("\n"))
}
