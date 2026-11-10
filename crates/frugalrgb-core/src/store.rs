//! Presets and settings on disk.
//!
//! The files and their JSON layout are the ones the Python app wrote
//! (`~/.frugalrgb_presets.json`, `~/.frugalrgb_config.json`), so existing
//! presets and calibration carry over unchanged.

use std::fs;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

use crate::engine::Card;
use crate::{Controller, Correction, DeviceInfo, Error, Mode, Result, Rgb, Zone};

/// The preset name that means "no preset".
pub const NO_PRESET: &str = "(none)";

pub(crate) fn home() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

pub fn presets_path() -> PathBuf {
    home().join(".frugalrgb_presets.json")
}

pub fn config_path() -> PathBuf {
    home().join(".frugalrgb_config.json")
}

fn default_true() -> bool {
    true
}

/// Saved state of one zone card.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ZoneState {
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Missing in hand-edited files: the preset's global colour applies.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub color: Option<Rgb>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Preset {
    /// Global colour, used for zones the preset does not mention.
    pub color: Rgb,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effect: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub speed: Option<f64>,
    /// Zone states keyed by [`zone_key`]. Empty in presets saved before
    /// per-zone colours existed.
    #[serde(default, skip_serializing_if = "IndexMap::is_empty")]
    pub devices: IndexMap<String, ZoneState>,
}

impl Preset {
    pub fn mode(&self) -> Mode {
        self.effect.as_deref().and_then(Mode::from_effect).unwrap_or(Mode::Static)
    }

    pub fn speed(&self) -> f64 {
        self.speed.unwrap_or(1.0)
    }

    /// The zone cards this preset describes for the detected devices. Zones
    /// the preset does not know are lit with its global colour.
    pub fn cards(&self, devices: &[DeviceInfo]) -> Vec<Card> {
        let mut cards = Vec::new();
        for (device, info) in devices.iter().enumerate() {
            for zone in &info.zones {
                let saved = self.devices.get(&zone_key(info, zone));
                cards.push(Card {
                    device,
                    zone: zone.id,
                    enabled: saved.map_or(true, |s| s.enabled),
                    color: saved.and_then(|s| s.color).unwrap_or(self.color),
                });
            }
        }
        cards
    }
}

/// Presets by name, in the order they were saved.
pub type Presets = IndexMap<String, Preset>;

/// The key a zone is stored under in a preset: the device name for
/// single-zone devices, `"device|zone"` otherwise.
pub fn zone_key(device: &DeviceInfo, zone: &Zone) -> String {
    if device.zones.len() > 1 {
        format!("{}|{}", device.name, zone.name)
    } else {
        device.name.clone()
    }
}

/// Per-device calibration. Old config files hold one triple for all devices.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Calibration {
    PerDevice(IndexMap<String, [f64; 3]>),
    Global([f64; 3]),
}

impl Default for Calibration {
    fn default() -> Self {
        Calibration::PerDevice(IndexMap::new())
    }
}

impl Calibration {
    pub fn for_device(&self, name: &str) -> Correction {
        match self {
            Calibration::PerDevice(map) => map.get(name).map(|c| Correction(*c)).unwrap_or_default(),
            Calibration::Global(c) => Correction(*c),
        }
    }
}

fn no_preset() -> String {
    NO_PRESET.to_string()
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub calibration: Calibration,
    #[serde(default)]
    pub off_on_close: bool,
    #[serde(default)]
    pub minimize_to_tray: bool,
    #[serde(default)]
    pub start_minimized: bool,
    #[serde(default = "no_preset")]
    pub startup_preset: String,
    /// Window size and position as `WIDTHxHEIGHT+X+Y`, in pixels.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub window_geometry: Option<String>,
    /// Settings this version does not know are kept as they are.
    #[serde(flatten)]
    pub other: IndexMap<String, serde_json::Value>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            calibration: Calibration::default(),
            off_on_close: false,
            minimize_to_tray: false,
            start_minimized: false,
            startup_preset: no_preset(),
            window_geometry: None,
            other: IndexMap::new(),
        }
    }
}

impl Config {
    /// The preset to apply at startup, if one is chosen.
    pub fn startup_preset(&self) -> Option<&str> {
        Some(self.startup_preset.as_str()).filter(|name| !name.is_empty() && *name != NO_PRESET)
    }

    /// The saved window size, if there is a sensible one.
    pub fn window_size(&self) -> Option<(u32, u32)> {
        let size = self.window_geometry.as_deref()?.split('+').next()?;
        let (width, height) = size.split_once('x')?;
        let (width, height) = (width.parse().ok()?, height.parse().ok()?);
        (width >= 200 && height >= 200).then_some((width, height))
    }

    pub fn set_window_size(&mut self, width: u32, height: u32) {
        self.window_geometry = Some(format!("{width}x{height}+0+0"));
    }

    /// Hand each device its calibration.
    pub fn calibrate(&self, controllers: &mut [Box<dyn Controller>]) {
        for ctrl in controllers {
            let correction = self.calibration.for_device(ctrl.name());
            ctrl.set_correction(correction);
        }
    }
}

fn load<T: Default + for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(T::default()),
        Err(e) => return Err(Error::Config(format!("{}: {e}", path.display()))),
    };
    serde_json::from_str(&text).map_err(|e| Error::Config(format!("{}: {e}", path.display())))
}

/// Write through a temporary file so a crash mid-write cannot leave a
/// truncated preset or config file behind.
fn save<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let text = serde_json::to_string_pretty(value)
        .map_err(|e| Error::Config(format!("{}: {e}", path.display())))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, text).map_err(|e| Error::Config(format!("{}: {e}", tmp.display())))?;
    fs::rename(&tmp, path).map_err(|e| Error::Config(format!("{}: {e}", path.display())))
}

/// A missing file is an empty preset list, not an error.
pub fn load_presets(path: &Path) -> Result<Presets> {
    load(path)
}

pub fn save_presets(path: &Path, presets: &Presets) -> Result<()> {
    save(path, presets)
}

/// A missing file is the default configuration.
pub fn load_config(path: &Path) -> Result<Config> {
    load(path)
}

pub fn save_config(path: &Path, config: &Config) -> Result<()> {
    save(path, config)
}
