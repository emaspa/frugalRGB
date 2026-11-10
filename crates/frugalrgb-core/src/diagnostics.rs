//! A plain-text report on this machine's RGB hardware, for bug reports.

use std::fmt::Write as _;
use std::path::PathBuf;

use crate::hid::HidBus;
use crate::transport::SharedBus;
use crate::{store, Controller, Error, Result};

/// SPD EEPROMs, temperature sensors and RGB chips all live in this range.
const SMBUS_SCAN: std::ops::Range<u8> = 0x50..0x78;

/// Whether the process may talk to privileged hardware interfaces.
pub fn is_elevated() -> bool {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: geteuid has no preconditions
        unsafe { libc::geteuid() == 0 }
    }
    #[cfg(windows)]
    {
        crate::win::is_admin()
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        false
    }
}

fn heading(out: &mut String, title: &str) {
    let _ = writeln!(out, "\n== {title} ==");
}

fn system(out: &mut String) {
    heading(out, "System");
    let _ = writeln!(out, "frugalRGB: {}", env!("CARGO_PKG_VERSION"));
    let _ = writeln!(out, "OS: {}", os_name());
    let _ = writeln!(out, "Architecture: {}", std::env::consts::ARCH);
    let _ = writeln!(out, "Administrator/root: {}", if is_elevated() { "yes" } else { "no" });

    #[cfg(target_os = "linux")]
    {
        let var = |name: &str| std::env::var(name).unwrap_or_else(|_| "?".into());
        let _ = writeln!(out, "Session: {} / {}", var("XDG_SESSION_TYPE"), var("XDG_CURRENT_DESKTOP"));
        let _ = writeln!(out, "I2C adapters:");
        let adapters = crate::smbus_linux::adapters();
        for adapter in &adapters {
            let _ = writeln!(out, "  i2c-{}: {}", adapter.number, adapter.name);
        }
        if adapters.is_empty() {
            let _ = writeln!(out, "  none (is the i2c-dev module loaded?)");
        }
    }
    #[cfg(windows)]
    {
        let path = crate::smbus_windows::library_path();
        let found = if path.exists() { "found" } else { "NOT FOUND" };
        let _ = writeln!(out, "PawnIO library: {found} ({})", path.display());
    }
}

#[cfg(target_os = "linux")]
fn os_name() -> String {
    let release = std::fs::read_to_string("/etc/os-release").unwrap_or_default();
    let distro = release
        .lines()
        .find_map(|line| line.strip_prefix("PRETTY_NAME="))
        .map_or("Linux", |name| name.trim_matches('"'))
        .to_string();
    let kernel = std::fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default();
    format!("{distro}, kernel {}", kernel.trim())
}

#[cfg(windows)]
fn os_name() -> String {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    std::process::Command::new("cmd")
        .args(["/C", "ver"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()
        .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string())
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| "Windows".into())
}

#[cfg(not(any(target_os = "linux", windows)))]
fn os_name() -> String {
    std::env::consts::OS.to_string()
}

fn hid_devices(out: &mut String) {
    heading(out, "USB HID devices");
    match HidBus::new() {
        Ok(hid) => {
            for line in hid.describe_all() {
                let _ = writeln!(out, "  {line}");
            }
        }
        Err(e) => {
            let _ = writeln!(out, "  {e}");
        }
    }
}

fn smbus_scan(out: &mut String, smbus: Option<&SharedBus>) {
    heading(out, &format!("SMBus scan (0x{:02X}-0x{:02X})", SMBUS_SCAN.start, SMBUS_SCAN.end - 1));
    let Some(bus) = smbus else {
        let _ = writeln!(out, "  SMBus not available");
        return;
    };
    let Ok(mut bus) = bus.lock() else {
        let _ = writeln!(out, "  SMBus is unusable after an earlier failure");
        return;
    };
    let mut answered = 0;
    for addr in SMBUS_SCAN {
        match bus.read_byte_data(addr, 0x00) {
            Ok(value) => {
                let _ = writeln!(out, "  0x{addr:02X}: responded (byte 0 = 0x{value:02X})");
                answered += 1;
            }
            // Linux refuses addresses a kernel driver has claimed (SPD)
            Err(e) if e.to_string().contains("busy") => {
                let _ = writeln!(out, "  0x{addr:02X}: in use by a kernel driver");
                answered += 1;
            }
            Err(_) => {}
        }
    }
    if answered == 0 {
        let _ = writeln!(out, "  nothing answered");
    }
}

fn devices(out: &mut String, controllers: &mut [Box<dyn Controller>]) {
    heading(out, "Detected devices");
    if controllers.is_empty() {
        let _ = writeln!(out, "  none");
    }
    for ctrl in controllers.iter() {
        let zones: Vec<&str> = ctrl.zones().iter().map(|z| z.name.as_str()).collect();
        let modes: Vec<&str> = ctrl.supported_modes().iter().map(|m| m.effect_name()).collect();
        let _ = writeln!(out, "  {}", ctrl.name());
        let _ = writeln!(out, "    Zones: {}", zones.join(", "));
        let _ = writeln!(out, "    Modes: {}", modes.join(", "));
        for (label, value) in ctrl.facts() {
            let _ = writeln!(out, "    {label}: {value}");
        }
    }
    for ctrl in controllers.iter_mut() {
        if let Some((title, text)) = ctrl.dump() {
            heading(out, &title);
            out.push_str(&text);
        }
    }
}

fn file(out: &mut String, title: &str, path: PathBuf) {
    heading(out, title);
    match std::fs::read_to_string(&path) {
        Ok(text) => out.push_str(text.trim_end()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => out.push_str("  (no file yet)"),
        Err(e) => {
            let _ = write!(out, "  {}: {e}", path.display());
        }
    }
    out.push('\n');
}

/// Collect everything worth knowing about the hardware into one text.
/// `app_log` is whatever the front end has logged so far.
pub fn report(controllers: &mut [Box<dyn Controller>], smbus: Option<&SharedBus>, app_log: &str) -> String {
    let mut out = String::from("frugalRGB diagnostics\n");
    system(&mut out);
    hid_devices(&mut out);
    smbus_scan(&mut out, smbus);
    devices(&mut out, controllers);
    file(&mut out, "Config", store::config_path());
    file(&mut out, "Presets", store::presets_path());
    if !app_log.is_empty() {
        heading(&mut out, "Log");
        out.push_str(app_log.trim_end());
        out.push('\n');
    }
    out
}

/// Write a report to the desktop, or to the home folder when there is no
/// desktop folder, and return where it went.
pub fn save(report: &str) -> Result<PathBuf> {
    let home = store::home();
    let desktop = home.join("Desktop");
    let folder = if desktop.is_dir() { desktop } else { home };
    let path = folder.join("frugalrgb_diagnostics.txt");
    std::fs::write(&path, report).map_err(|e| Error::Io(format!("{}: {e}", path.display())))?;
    Ok(path)
}
