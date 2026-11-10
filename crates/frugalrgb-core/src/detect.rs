//! Finds every supported device on this machine.

use crate::controllers::{asrock, aura, gigabyte, katana, msi};
use crate::hid::HidBus;
use crate::transport::HidTransport;
use crate::Controller;

/// Scan for all supported RGB controllers. Devices that are absent, busy or
/// not permitted are skipped: a missing device is never an error.
pub fn detect_all() -> Vec<Box<dyn Controller>> {
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

    #[cfg(target_os = "linux")]
    {
        controllers.extend(linux::detect_asus_gpu());
        controllers.extend(linux::detect_memory());
    }
    #[cfg(not(target_os = "linux"))]
    log::debug!("SMBus devices (memory, GPU) are not supported on this platform yet");

    for ctrl in &controllers {
        log::info!("  Found: {}", ctrl.name());
    }
    if controllers.is_empty() {
        log::warn!("No RGB devices found");
    }
    controllers
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
        if info.usage_page != msi::USAGE_PAGE || info.usage != msi::USAGE {
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
    for info in hid.devices(gigabyte::VID) {
        // Match by HID usage or known PID, not by interface number: the RGB
        // collection sits on a different interface per chip and OS, and some
        // backends report no usage at all. The info request below verifies.
        let by_usage = info.usage_page == gigabyte::USAGE_PAGE && info.usage == gigabyte::USAGE;
        if !by_usage && !gigabyte::KNOWN_PIDS.contains(&info.product_id) {
            continue;
        }

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

#[cfg(target_os = "linux")]
mod linux {
    use std::fs;
    use std::sync::{Arc, Mutex};

    use crate::controllers::ene::{self, EneV2, SharedBus};
    use crate::smbus_linux::{adapters, chipset_adapter, LinuxSmBus};
    use crate::Controller;

    const ASUS_SUBSYSTEM_VENDOR: u16 = 0x1043;

    /// ASUS card names by PCI subsystem device ID.
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
            let label = match KNOWN_CARDS.iter().find(|(id, _)| *id == device) {
                Some((_, card)) => format!("ASUS {card}"),
                None => format!("ASUS GPU RGB (NVIDIA GPU {slot})"),
            };
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

    /// RGB memory on the chipset SMBus: DDR5 first, then classic ENE DRAM on
    /// the addresses DDR5 did not claim.
    pub fn detect_memory() -> Vec<Box<dyn Controller>> {
        let Some(adapter) = chipset_adapter() else {
            log::warn!(
                "No SMBus adapter found (memory RGB unavailable). Is the chipset SMBus kernel \
                 module loaded (i2c-piix4 on AMD, i2c-i801 on Intel)?"
            );
            return Vec::new();
        };
        let bus = match LinuxSmBus::open(adapter.number) {
            Ok(bus) => bus,
            Err(e) => {
                log::warn!(
                    "SMBus unavailable (memory RGB): {e}. Add your user to the 'i2c' group or run as root."
                );
                return Vec::new();
            }
        };
        log::info!("Using SMBus adapter i2c-{} ({})", adapter.number, adapter.name);

        let shared: SharedBus = Arc::new(Mutex::new(bus));
        let mut found: Vec<Box<dyn Controller>> = Vec::new();
        let mut claimed = Vec::new();
        for (addr, ctrl) in ene::detect_ddr5(&shared) {
            claimed.push(addr);
            found.push(Box::new(ctrl));
        }
        for ctrl in ene::detect_dram(&shared, &claimed) {
            found.push(Box::new(ctrl));
        }
        found
    }
}
