use std::fmt;

/// A colour as `[red, green, blue]`.
pub type Rgb = [u8; 3];

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug)]
pub enum Error {
    /// The device or bus rejected or dropped a transfer.
    Io(String),
    /// The device answered with something the protocol does not allow.
    Protocol(String),
    /// A preset or config file could not be read or understood.
    Config(String),
    /// The device has no such capability.
    Unsupported(&'static str),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(m) | Error::Protocol(m) | Error::Config(m) => f.write_str(m),
            Error::Unsupported(what) => write!(f, "{what} is not supported by this device"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e.to_string())
    }
}

/// Lighting modes shared by every device. A device that cannot run a mode in
/// hardware is animated by the software loop in [`crate::engine`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Mode {
    Off,
    Static,
    Breathing,
    ColorCycle,
    Rainbow,
    Strobe,
}

impl Mode {
    pub const ALL: [Mode; 6] = [
        Mode::Off,
        Mode::Static,
        Mode::Breathing,
        Mode::ColorCycle,
        Mode::Rainbow,
        Mode::Strobe,
    ];

    /// Parse the effect name used in preset files (`"color_cycle"`, ...).
    pub fn from_effect(name: &str) -> Option<Mode> {
        Mode::ALL.into_iter().find(|m| m.effect_name() == name)
    }

    pub fn effect_name(self) -> &'static str {
        match self {
            Mode::Off => "off",
            Mode::Static => "static",
            Mode::Breathing => "breathing",
            Mode::ColorCycle => "color_cycle",
            Mode::Rainbow => "rainbow",
            Mode::Strobe => "strobe",
        }
    }

    pub fn is_animated(self) -> bool {
        !matches!(self, Mode::Off | Mode::Static)
    }
}

/// One controllable zone of a device.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Zone {
    pub id: u8,
    pub name: String,
    /// A pseudo-zone that addresses every LED on the device ("All Zones"),
    /// as opposed to one physical zone.
    pub is_all: bool,
}

impl Zone {
    pub fn new(id: u8, name: impl Into<String>) -> Self {
        Zone { id, name: name.into(), is_all: false }
    }

    pub fn all(id: u8, name: impl Into<String>) -> Self {
        Zone { id, name: name.into(), is_all: true }
    }
}

/// The state one zone should take: lit with a colour, or dark (`None`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ZoneWrite {
    pub zone: u8,
    pub color: Option<Rgb>,
}

impl ZoneWrite {
    pub fn lit(zone: u8, color: Rgb) -> Self {
        ZoneWrite { zone, color: Some(color) }
    }

    pub fn off(zone: u8) -> Self {
        ZoneWrite { zone, color: None }
    }
}

/// Per-channel brightness multipliers that compensate for LED imbalance.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Correction(pub [f64; 3]);

impl Default for Correction {
    fn default() -> Self {
        Correction([1.0; 3])
    }
}

impl Correction {
    pub fn apply(&self, color: Rgb) -> Rgb {
        // Truncate, then clamp: a multiplier above 1.0 must not wrap
        let scale = |v: u8, k: f64| ((f64::from(v) * k) as u32).min(255) as u8;
        [
            scale(color[0], self.0[0]),
            scale(color[1], self.0[1]),
            scale(color[2], self.0[2]),
        ]
    }
}
