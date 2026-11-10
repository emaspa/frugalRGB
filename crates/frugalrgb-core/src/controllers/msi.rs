//! MSI Mystic Light over USB HID, 185-byte protocol.
//!
//! The whole lighting state is one 185-byte feature report (ID 0x52). It is
//! read once, edited in place per zone, and written back as a whole.

use super::Controller;
use crate::transport::HidTransport;
use crate::{Correction, Mode, Result, Rgb, Zone, ZoneWrite};

pub const VID: u16 = 0x1462;
pub const REPORT_ID: u8 = 0x52;
pub const PACKET_SIZE: usize = 185;

/// Boards on this protocol publish this HID usage.
pub const USAGE_PAGE: u16 = 0x0001;
pub const USAGE: u16 = 0x00;

const MODE_DISABLE: u8 = 0x00;
const MODE_STATIC: u8 = 0x01;
const MODE_BREATHING: u8 = 0x02;
const MODE_FLASHING: u8 = 0x03;
const MODE_COLOR_SHIFT: u8 = 0x15;
const MODE_RAINBOW: u8 = 0x19;

/// Brightness 0-10; 10 is 100%.
const BRIGHTNESS: u8 = 10;
/// Byte that asks the board to store the state in flash.
const SAVE_FLAG: usize = 184;

const MODES: [Mode; 6] = [
    Mode::Off,
    Mode::Static,
    Mode::Breathing,
    Mode::Strobe,
    Mode::ColorCycle,
    Mode::Rainbow,
];

/// A block of the state report.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Slot {
    JRgb1,
    JRgb2,
    JPipe1,
    JPipe2,
    JRainbow1,
    JRainbow2,
    Onboard,
    /// One of the nine individual onboard LEDs, 1-based.
    OnboardLed(u8),
}

impl Slot {
    fn offset(self) -> usize {
        match self {
            Slot::JRgb1 => 1,
            Slot::JPipe1 => 11,
            Slot::JPipe2 => 21,
            Slot::JRainbow1 => 31,
            Slot::JRainbow2 => 42,
            Slot::Onboard => 74,
            Slot::OnboardLed(n) => 74 + 10 * usize::from(n),
            Slot::JRgb2 => 174,
        }
    }

    /// Rainbow headers have an eleventh byte: the LED count.
    fn is_rainbow_header(self) -> bool {
        matches!(self, Slot::JRainbow1 | Slot::JRainbow2)
    }
}

/// What a zone of the device maps to in the state report.
#[derive(Clone, Copy)]
enum Target {
    Everything,
    Onboard,
    Single(Slot),
}

/// A board this implementation knows the layout of.
pub struct Board {
    pub pid: u16,
    pub name: &'static str,
    pub onboard_leds: u8,
    pub jrainbow1: bool,
}

/// Unknown PIDs still work through the "All Zones" card alone.
pub const BOARDS: [Board; 1] =
    [Board { pid: 0x7E03, name: "MSI MPG Z790I EDGE WIFI", onboard_leds: 6, jrainbow1: true }];

pub fn board(pid: u16) -> Option<&'static Board> {
    BOARDS.iter().find(|b| b.pid == pid)
}

/// Speed 0 (fastest) to 5 as the board's 0 = low, 1 = medium, 2 = high.
fn speed_value(speed: u8) -> u8 {
    match speed {
        0 | 1 => 2,
        2 | 3 => 1,
        4 | 5 => 0,
        _ => 1,
    }
}

fn mode_value(mode: Mode) -> u8 {
    match mode {
        Mode::Off => MODE_DISABLE,
        Mode::Static => MODE_STATIC,
        Mode::Breathing => MODE_BREATHING,
        Mode::Strobe => MODE_FLASHING,
        Mode::ColorCycle => MODE_COLOR_SHIFT,
        Mode::Rainbow => MODE_RAINBOW,
    }
}

pub struct Msi {
    dev: Box<dyn HidTransport>,
    name: String,
    onboard_leds: u8,
    state: Vec<u8>,
    zones: Vec<Zone>,
    targets: Vec<Target>,
    mode: Mode,
    speed: u8,
    correction: Correction,
}

impl Msi {
    /// Whether `dev` hands out a state report long enough to be this
    /// protocol. Older boards answer with a shorter one.
    pub fn responds(dev: &mut dyn HidTransport) -> bool {
        matches!(dev.get_feature_report(REPORT_ID, PACKET_SIZE), Ok(state) if state.len() >= 100)
    }

    pub fn new(mut dev: Box<dyn HidTransport>, pid: u16) -> Result<Self> {
        let known = board(pid);
        let name = known.map_or_else(|| format!("MSI Mystic Light (0x{pid:04X})"), |b| b.name.to_string());
        let onboard_leds = known.map_or(0, |b| b.onboard_leds);

        let state = match dev.get_feature_report(REPORT_ID, PACKET_SIZE) {
            Ok(mut state) => {
                if state.len() < PACKET_SIZE {
                    log::warn!("MSI: got {} bytes (expected {PACKET_SIZE}), padding", state.len());
                    state.resize(PACKET_SIZE, 0);
                }
                state
            }
            Err(e) => {
                log::error!("MSI: failed to read state: {e}");
                let mut blank = vec![0u8; PACKET_SIZE];
                blank[0] = REPORT_ID;
                blank
            }
        };

        let mut zones = Vec::new();
        let mut targets = Vec::new();
        if state[0] == REPORT_ID {
            zones.push(Zone::all(0, "All Zones"));
            targets.push(Target::Everything);
            if onboard_leds > 0 {
                zones.push(Zone::new(1, "Onboard LEDs"));
                targets.push(Target::Onboard);
            }
            if known.is_some_and(|b| b.jrainbow1) {
                zones.push(Zone::new(2, "JRAINBOW1"));
                targets.push(Target::Single(Slot::JRainbow1));
            }
        }
        log::info!("MSI Mystic Light: {} zone(s) detected", zones.len());

        Ok(Msi {
            dev,
            name,
            onboard_leds,
            state,
            zones,
            targets,
            mode: Mode::Static,
            speed: 1,
            correction: Correction::default(),
        })
    }

    /// Write one zone block of the state report.
    fn set_slot(&mut self, slot: Slot, mode: Mode, [r, g, b]: Rgb) {
        let at = slot.offset();
        // Colour-flag bit 7 selects the user colour instead of the rainbow
        let mut color_flags = if mode == Mode::Rainbow { 0x00 } else { 0x80 };
        if slot == Slot::Onboard {
            color_flags |= 0x01; // the individual onboard LEDs follow this one
        }

        self.state[at] = mode_value(mode);
        self.state[at + 1..at + 4].copy_from_slice(&[r, g, b]);
        self.state[at + 4] = ((BRIGHTNESS & 0x1F) << 2) | (self.speed & 0x03);
        self.state[at + 5..at + 8].copy_from_slice(&[r, g, b]); // second colour
        self.state[at + 8] = color_flags;
        self.state[at + 9] = 0x00;
        if slot.is_rainbow_header() {
            self.state[at + 10] = 100;
        }
    }

    fn onboard_slots(&self) -> impl Iterator<Item = Slot> {
        std::iter::once(Slot::Onboard).chain((1..=self.onboard_leds.min(9)).map(Slot::OnboardLed))
    }

    fn set_target(&mut self, target: Target, mode: Mode, color: Rgb) {
        let slots: Vec<Slot> = match target {
            Target::Everything => self
                .onboard_slots()
                .chain([Slot::JRgb1, Slot::JRgb2, Slot::JPipe1, Slot::JPipe2, Slot::JRainbow1, Slot::JRainbow2])
                .collect(),
            Target::Onboard => self.onboard_slots().collect(),
            Target::Single(slot) => vec![slot],
        };
        for slot in slots {
            self.set_slot(slot, mode, color);
        }
    }

    fn send(&mut self, save: bool) -> Result<()> {
        self.state[SAVE_FLAG] = u8::from(save);
        self.dev.send_feature_report(&self.state)
    }
}

impl Controller for Msi {
    fn name(&self) -> &str {
        &self.name
    }

    fn zones(&self) -> &[Zone] {
        &self.zones
    }

    fn supported_modes(&self) -> &'static [Mode] {
        &MODES
    }

    fn hardware_animates(&self, mode: Mode) -> bool {
        mode.is_animated()
    }

    fn set_correction(&mut self, correction: Correction) {
        self.correction = correction;
    }

    fn set_mode(&mut self, mode: Mode, speed: u8) -> Result<()> {
        self.mode = mode;
        self.speed = speed_value(speed);
        Ok(())
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        for zw in zones {
            let Some(index) = self.zones.iter().position(|z| z.id == zw.zone) else {
                continue;
            };
            let target = self.targets[index];
            // The mode is per zone, so a dark zone is a disabled one
            match zw.color {
                Some(color) => self.set_target(target, self.mode, self.correction.apply(color)),
                None => self.set_target(target, Mode::Off, [0, 0, 0]),
            }
        }
        self.send(false)
    }

    fn supports_save(&self) -> bool {
        true
    }

    fn save(&mut self) -> Result<()> {
        self.send(true)
    }
}
