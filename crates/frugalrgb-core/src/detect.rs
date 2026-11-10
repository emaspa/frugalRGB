//! Finds every supported device on this machine.

use crate::controllers::{asrock, aura, gigabyte, katana, msi};
use crate::hid::HidBus;
use crate::transport::{HidTransport, SharedBus};
use crate::Controller;

/// What a scan found.
pub struct Hardware {
    pub controllers: Vec<Box<dyn Controller>>,
    /// The chipset SMBus the memory sits on, when it could be opened.
    pub smbus: Option<SharedBus>,
}

/// Scan for all supported RGB controllers. Devices that are absent, busy or
/// not permitted are skipped: a missing device is never an error.
pub fn scan() -> Hardware {
    let mut controllers: Vec<Box<dyn Controller>> = Vec::new();
    log::info!("Scanning for RGB devices...");

    match HidBus::new() {
        Ok(hid) => {
            controllers.extend(detect_asrock(&hid));
            controllers.extend(detect_msi(&hid));
            controllers.extend(detect_gigabyte(&hid));
            controllers.extend(detect_aura(&hid));
            controllers.extend(detect_katana(&hid));
        }
        Err(e) => log::warn!("USB devices unavailable: {e}"),
    }

    #[cfg(any(target_os = "linux", windows))]
    let smbus = {
        controllers.extend(platform::detect_asus_gpu());
        let smbus = platform::open_smbus();
        if let Some(bus) = &smbus {
            controllers.extend(memory_on(bus));
        }
        smbus
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    let smbus = None;

    for ctrl in &controllers {
        log::info!("  Found: {}", ctrl.name());
    }
    if controllers.is_empty() {
        log::warn!("No RGB devices found");
    }
    Hardware { controllers, smbus }
}

/// [`scan`] for callers that only want the devices.
pub fn detect_all() -> Vec<Box<dyn Controller>> {
    scan().controllers
}

fn boxed(ctrl: impl Controller + 'static) -> Option<Box<dyn Controller>> {
    Some(Box::new(ctrl))
}

/// ASRock Polychrome USB.
fn detect_asrock(hid: &HidBus) -> Option<Box<dyn Controller>> {
    let info = hid.devices(asrock::VID).into_iter().find(|d| d.product_id == asrock::PID)?;
    let ctrl = hid.open(&info).and_then(|dev| asrock::Asrock::new(Box::new(dev)));
    match ctrl {
        Ok(ctrl) => {
            log::info!("Found ASRock Polychrome USB at {}", info.path());
            boxed(ctrl)
        }
        Err(e) => {
            log::warn!("Failed to open ASRock Polychrome USB: {e}");
            None
        }
    }
}

/// MSI Mystic Light, 185-byte protocol.
fn detect_msi(hid: &HidBus) -> Option<Box<dyn Controller>> {
    for info in hid.devices(msi::VID) {
        if !info.has_usage(msi::USAGE_PAGE, msi::USAGE) {
            continue;
        }
        let is_mystic = info.product.to_uppercase().contains("MYSTIC LIGHT");
        if !is_mystic && msi::board(info.product_id).is_none() {
            continue;
        }

        let mut dev = match hid.open(&info) {
            Ok(dev) => dev,
            Err(e) => {
                log::warn!("Failed to open MSI Mystic Light (PID=0x{:04X}): {e}", info.product_id);
                continue;
            }
        };
        if !msi::Msi::responds(&mut dev) {
            log::debug!("MSI PID 0x{:04X}: state report too short, skipping", info.product_id);
            continue;
        }
        log::info!(
            "Found MSI Mystic Light at {} (PID=0x{:04X}, {})",
            info.path(),
            info.product_id,
            info.product
        );
        match msi::Msi::new(Box::new(dev), info.product_id) {
            Ok(ctrl) => return boxed(ctrl),
            Err(e) => log::warn!("Failed to open MSI Mystic Light (PID=0x{:04X}): {e}", info.product_id),
        }
    }
    None
}

/// Gigabyte RGB Fusion 2.0 (ITE IT5711 / IT8297 family).
fn detect_gigabyte(hid: &HidBus) -> Option<Box<dyn Controller>> {
    // Match by HID usage, not by interface number: the RGB collection sits on
    // a different interface per chip and OS. An interface of a known chip
    // whose usages the backend cannot tell is tried too, after the others:
    // asking a keyboard-style interface for the info report can stall for
    // the five seconds a USB control transfer may take to time out.
    let by_usage = |info: &crate::hid::HidInfo| info.has_usage(gigabyte::USAGE_PAGE, gigabyte::USAGE);
    let mut candidates: Vec<_> = hid
        .devices(gigabyte::VID)
        .into_iter()
        .filter(|info| {
            by_usage(info) || (info.usages.is_empty() && gigabyte::KNOWN_PIDS.contains(&info.product_id))
        })
        .collect();
    candidates.sort_by_key(|info| !by_usage(info));

    for info in candidates {

        let mut dev = match hid.open(&info) {
            Ok(dev) => dev,
            Err(e) => {
                log::debug!("Gigabyte RGB candidate {} rejected: {e}", info.path());
                continue;
            }
        };
        if !gigabyte::Gigabyte::responds(&mut dev) {
            // Expected for the non-RGB interfaces of a known PID
            log::debug!("Gigabyte PID 0x{:04X}: no product string, skipping", info.product_id);
            continue;
        }

        log::info!(
            "Found Gigabyte RGB Fusion 2.0 at {} (PID=0x{:04X}, {})",
            info.path(),
            info.product_id,
            info.product
        );
        match gigabyte::Gigabyte::new(Box::new(dev), info.product_id) {
            Ok(ctrl) => return boxed(ctrl),
            Err(e) => log::debug!("Gigabyte RGB candidate {} rejected: {e}", info.path()),
        }
    }
    None
}

/// ASUS Aura mainboard RGB.
fn detect_aura(hid: &HidBus) -> Option<Box<dyn Controller>> {
    // The device can expose several HID interfaces and only one answers the
    // config query. Prefer that one; keep the first that opens as a fallback.
    let mut fallback: Option<aura::Aura> = None;

    for info in hid.devices(aura::VID) {
        let experimental = aura::EXPERIMENTAL_PIDS.contains(&info.product_id);
        if !experimental && !aura::KNOWN_PIDS.contains(&info.product_id) {
            continue;
        }
        if experimental {
            log::warn!("ASUS Aura PID 0x{:04X} is experimental, please report results", info.product_id);
        }

        let ctrl = hid
            .open(&info)
            .and_then(|dev| aura::Aura::new(Box::new(dev), info.product_id, &info.product));
        match ctrl {
            Ok(ctrl) if ctrl.config_ok() => {
                log::info!(
                    "Found ASUS Aura USB at {} (PID=0x{:04X}, fw={}, {})",
                    info.path(),
                    info.product_id,
                    ctrl.firmware(),
                    info.product
                );
                return boxed(ctrl);
            }
            Ok(ctrl) => {
                fallback.get_or_insert(ctrl);
            }
            Err(e) => log::warn!(
                "Failed to open ASUS Aura USB (PID=0x{:04X}): {e}. Is Armoury Crate / ASUS \
                 LightingService running?",
                info.product_id
            ),
        }
    }

    let ctrl = fallback?;
    log::info!("Found ASUS Aura USB but could not read its config table, registering a single onboard zone");
    boxed(ctrl)
}

/// Creative Sound BlasterX Katana soundbar.
fn detect_katana(hid: &HidBus) -> Option<Box<dyn Controller>> {
    for info in hid.devices(katana::VID) {
        if info.product_id != katana::PID {
            continue;
        }
        // The command channel is the 64-byte HID interface; the probe
        // verifies when the backend cannot report interface numbers
        if info.interface != katana::COMMAND_INTERFACE && info.interface != -1 {
            continue;
        }

        let dev: Box<dyn HidTransport> = match hid.open(&info) {
            Ok(dev) => Box::new(dev),
            Err(e) => {
                log::debug!("Katana candidate rejected: {e}");
                continue;
            }
        };
        match katana::Katana::probe(dev) {
            Ok(Some(ctrl)) => return boxed(ctrl),
            Ok(None) => {}
            Err(e) => log::debug!("Katana candidate rejected: {e}"),
        }
    }
    None
}

/// RGB memory on the chipset SMBus: DDR5 first, then classic ENE DRAM on
/// the addresses DDR5 did not claim.
#[cfg(any(target_os = "linux", windows))]
fn memory_on(bus: &SharedBus) -> Vec<Box<dyn Controller>> {
    use crate::controllers::ene;

    let mut found: Vec<Box<dyn Controller>> = Vec::new();
    let mut claimed = Vec::new();
    for (addr, ctrl) in ene::detect_ddr5(bus) {
        claimed.push(addr);
        found.push(Box::new(ctrl));
    }
    for ctrl in ene::detect_dram(bus, &claimed) {
        found.push(Box::new(ctrl));
    }
    found
}

#[cfg(any(target_os = "linux", windows))]
const ASUS_SUBSYSTEM_VENDOR: u16 = 0x1043;

/// ASUS card names by PCI subsystem device ID.
#[cfg(any(target_os = "linux", windows))]
const KNOWN_CARDS: [(u16, &str); 14] = [
    (0x89EE, "TUF RTX 5090 OC"),
    (0x89EF, "TUF RTX 5090"),
    (0x89D7, "TUF RTX 5080 OC"),
    (0x89F4, "TUF RTX 5070 Ti OC"),
    (0x89F2, "TUF RTX 5070"),
    (0x89E3, "ROG ASTRAL RTX 5090 OC"),
    (0x8A3C, "ROG ASTRAL RTX 5090 OC BTF"),
    (0x8A2E, "ROG ASTRAL RTX 5090 OC WHITE"),
    (0x8A61, "ROG MATRIX PLATINUM RTX 5090"),
    (0x8919, "TUF RTX 4090 OC"),
    (0x891A, "TUF RTX 4090"),
    (0x88F3, "TUF RTX 4080 OC"),
    (0x88F4, "TUF RTX 4080"),
    (0x88C7, "TUF RTX 4070 Ti OC"),
];

/// The device name of an ASUS card: its model when known, else `gpu`.
#[cfg(any(target_os = "linux", windows))]
fn card_label(subsystem_device: u16, gpu: &str) -> String {
    match KNOWN_CARDS.iter().find(|(id, _)| *id == subsystem_device) {
        Some((_, card)) => format!("ASUS {card}"),
        None => format!("ASUS GPU RGB ({gpu})"),
    }
}

#[cfg(target_os = "linux")]
mod platform {
    use std::fs;
    use std::sync::{Arc, Mutex};

    use super::{card_label, ASUS_SUBSYSTEM_VENDOR};
    use crate::controllers::ene::{self, EneV2};
    use crate::smbus_linux::{adapters, chipset_adapter, LinuxSmBus};
    use crate::transport::SharedBus;
    use crate::Controller;

    fn read_hex(path: std::path::PathBuf) -> Option<u16> {
        let text = fs::read_to_string(path).ok()?;
        u16::from_str_radix(text.trim().trim_start_matches("0x"), 16).ok()
    }

    /// The ENE chip of an ASUS graphics card, reached through the I2C
    /// adapters the NVIDIA driver exposes.
    pub fn detect_asus_gpu() -> Option<Box<dyn Controller>> {
        for adapter in adapters() {
            if !adapter.name.starts_with("NVIDIA i2c adapter") {
                continue;
            }
            // The adapter's parent in sysfs is the card's PCI device
            let Some(pci) = fs::canonicalize(&adapter.sysfs).ok().and_then(|p| p.parent().map(|p| p.to_path_buf()))
            else {
                continue;
            };
            let (Some(vendor), Some(device)) =
                (read_hex(pci.join("subsystem_vendor")), read_hex(pci.join("subsystem_device")))
            else {
                continue;
            };
            if vendor != ASUS_SUBSYSTEM_VENDOR {
                continue;
            }

            let mut bus = match LinuxSmBus::open(adapter.number) {
                Ok(bus) => bus,
                Err(e) => {
                    log::debug!("Cannot open i2c-{} ({}): {e}", adapter.number, adapter.name);
                    continue;
                }
            };
            let Some(chip) = ene::read_name(&mut bus, ene::GPU_ADDR) else {
                continue;
            };

            let slot = pci.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let label = card_label(device, &format!("NVIDIA GPU {slot}"));
            log::info!(
                "ASUS GPU found: {slot} (subsys 0x{vendor:04X}:0x{device:04X}) on i2c-{}, ENE chip {chip}",
                adapter.number
            );

            let shared: SharedBus = Arc::new(Mutex::new(bus));
            match EneV2::gpu(shared, &label) {
                Ok(ctrl) => return Some(Box::new(ctrl)),
                Err(e) => log::debug!("ASUS GPU RGB on i2c-{} rejected: {e}", adapter.number),
            }
        }
        None
    }

    /// The chipset SMBus, where the memory sits.
    pub fn open_smbus() -> Option<SharedBus> {
        let Some(adapter) = chipset_adapter() else {
            log::warn!(
                "No SMBus adapter found (memory RGB unavailable). Is the chipset SMBus kernel \
                 module loaded (i2c-piix4 on AMD, i2c-i801 on Intel)?"
            );
            return None;
        };
        match LinuxSmBus::open(adapter.number) {
            Ok(bus) => {
                log::info!("Using SMBus adapter i2c-{} ({})", adapter.number, adapter.name);
                Some(Arc::new(Mutex::new(bus)))
            }
            Err(e) => {
                log::warn!("SMBus unavailable (memory RGB): {e}. Add your user to the 'i2c' group or run as root.");
                None
            }
        }
    }
}

#[cfg(windows)]
mod platform {
    use std::sync::{Arc, Mutex};

    use super::{card_label, ASUS_SUBSYSTEM_VENDOR};
    use crate::controllers::ene::{self, EneV2};
    use crate::nvapi::{NvApi, NvI2c};
    use crate::smbus_windows::PawnIoSmBus;
    use crate::transport::SharedBus;
    use crate::Controller;

    /// The I2C port of an NVIDIA card that ASUS wires the RGB chip to.
    const RGB_PORT: u8 = 1;

    /// The ENE chip of an ASUS graphics card, reached through NvAPI.
    pub fn detect_asus_gpu() -> Option<Box<dyn Controller>> {
        let api = match NvApi::open() {
            Ok(api) => api,
            Err(e) => {
                log::debug!("NvAPI unavailable: {e}");
                return None;
            }
        };
        let gpus = match api.gpus() {
            Ok(gpus) => gpus,
            Err(e) => {
                log::debug!("{e}");
                return None;
            }
        };

        for gpu in gpus {
            if gpu.subsystem_vendor != ASUS_SUBSYSTEM_VENDOR {
                continue;
            }
            let mut bus = NvI2c::new(Arc::clone(&api), &gpu, RGB_PORT);
            let Some(chip) = ene::read_name(&mut bus, ene::GPU_ADDR) else {
                log::debug!("No ENE controller at 0x{:02X} on {}", ene::GPU_ADDR, gpu.name);
                continue;
            };
            log::info!(
                "ASUS GPU found: {} (subsys 0x{:04X}:0x{:04X}), ENE chip {chip}",
                gpu.name,
                gpu.subsystem_vendor,
                gpu.subsystem_device
            );

            let shared: SharedBus = Arc::new(Mutex::new(bus));
            match EneV2::gpu(shared, &card_label(gpu.subsystem_device, &gpu.name)) {
                Ok(ctrl) => return Some(Box::new(ctrl)),
                Err(e) => log::debug!("ASUS GPU RGB on {} rejected: {e}", gpu.name),
            }
        }
        None
    }

    /// The chipset SMBus, where the memory sits. Needs PawnIO and an
    /// elevated process.
    pub fn open_smbus() -> Option<SharedBus> {
        if !crate::win::is_admin() {
            log::warn!(
                "Not running as Administrator: memory RGB is unavailable. USB devices \
                 (motherboard RGB) still work."
            );
            return None;
        }
        match PawnIoSmBus::open() {
            Ok(bus) => Some(Arc::new(Mutex::new(bus))),
            Err(e) => {
                log::warn!("SMBus unavailable (memory RGB): {e}");
                None
            }
        }
    }
}
