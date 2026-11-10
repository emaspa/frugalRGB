//! One module per device family. Each controller owns its transport and
//! turns [`ZoneWrite`]s into that device's packets.

use crate::{Correction, Error, Mode, Result, Zone, ZoneWrite};

pub mod asrock;
pub mod aura;
pub mod ene;
pub mod gigabyte;
pub mod katana;
pub mod msi;

/// Bytes as rows of sixteen hex values, each row prefixed with its offset.
pub(crate) fn hex_rows(bytes: &[u8], base: usize) -> String {
    let mut text = String::new();
    for (row, chunk) in bytes.chunks(16).enumerate() {
        let hex: Vec<String> = chunk.iter().map(|b| format!("{b:02X}")).collect();
        text.push_str(&format!("  0x{:04X}: {}\n", base + row * 16, hex.join(" ")));
    }
    text
}

/// What a front end needs to know about a device, detached from the
/// hardware handle so it can be used while a worker thread owns the device.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceInfo {
    pub name: String,
    pub zones: Vec<Zone>,
    pub supports_save: bool,
}

impl DeviceInfo {
    pub fn of(ctrl: &dyn Controller) -> Self {
        DeviceInfo {
            name: ctrl.name().to_string(),
            zones: ctrl.zones().to_vec(),
            supports_save: ctrl.supports_save(),
        }
    }

    pub fn all(controllers: &[Box<dyn Controller>]) -> Vec<Self> {
        controllers.iter().map(|c| Self::of(c.as_ref())).collect()
    }
}

pub trait Controller: Send {
    /// Human-readable device name. Preset files key on it, so it is stable.
    fn name(&self) -> &str;

    fn zones(&self) -> &[Zone];

    fn supported_modes(&self) -> &'static [Mode];

    /// True when the device animates `mode` on its own. Otherwise the
    /// software loop streams frames to it.
    fn hardware_animates(&self, mode: Mode) -> bool;

    fn set_correction(&mut self, correction: Correction);

    /// Select the mode the following [`write_zones`](Self::write_zones)
    /// calls are rendered in. Speed runs from 0 (fastest) to 5 (slowest).
    fn set_mode(&mut self, mode: Mode, speed: u8) -> Result<()>;

    /// Write zone states in order and commit them. A zone with no colour is
    /// driven dark even when the active mode ignores colours.
    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()>;

    /// Set the mode, then write and commit the zones.
    fn apply(&mut self, mode: Mode, speed: u8, zones: &[ZoneWrite]) -> Result<()> {
        self.set_mode(mode, speed)?;
        self.write_zones(zones)
    }

    /// Short facts about the device for display: firmware, chip, address.
    fn facts(&self) -> Vec<(&'static str, String)> {
        Vec::new()
    }

    /// The device's tables and registers for a diagnostics report, as a
    /// section title and its text. Reads from the device; changes nothing.
    fn dump(&mut self) -> Option<(String, String)> {
        None
    }

    /// Whether [`save`](Self::save) can store the current state in the
    /// device's flash so it survives a power cycle.
    fn supports_save(&self) -> bool {
        false
    }

    fn save(&mut self) -> Result<()> {
        Err(Error::Unsupported("saving to hardware"))
    }
}
