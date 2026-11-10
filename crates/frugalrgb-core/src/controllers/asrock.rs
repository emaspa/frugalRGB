//! ASRock Polychrome RGB over USB HID (VID 0x26CE).
//!
//! Every command is a 65-byte output report answered by a 64-byte input
//! report. Zones and their LED counts come from config tables on the device.

use super::Controller;
use crate::transport::HidTransport;
use crate::{Correction, Mode, Result, Rgb, Zone, ZoneWrite};

pub const VID: u16 = 0x26CE;
pub const PID: u16 = 0x01A2;

const REPORT_SIZE: usize = 65; // report ID + 64 data bytes

const CMD_SET_ZONE: u8 = 0x10;
const CMD_COMMIT: u8 = 0x12;
const CMD_READ_HEADER: u8 = 0x14;
const CMD_WRITE_HEADER: u8 = 0x15;

const CFG_ZONE_AVAIL: u8 = 0x01;
const CFG_LED_COUNT: u8 = 0x02;
const CFG_RGSWAP: u8 = 0x03;

/// LED count that marks a zone as not present.
const ZONE_UNAVAILABLE: u8 = 0x1E;
/// Speed runs from 0x00 (fast) to 0xFF (slow).
const SPEED_DEFAULT: u8 = 0xE0;

/// Zone names by hardware zone number, as OpenRGB numbers them.
const ZONE_NAMES: [&str; 8] = [
    "RGB Header 1",
    "RGB Header 2",
    "ARGB Header 1",
    "ARGB Header 2",
    "PCH",
    "IO Cover",
    "PCB",
    "Audio/ARGB3",
];

const MODES: [Mode; 6] = [
    Mode::Off,
    Mode::Static,
    Mode::Breathing,
    Mode::Strobe,
    Mode::ColorCycle,
    Mode::Rainbow,
];

fn mode_value(mode: Mode) -> u8 {
    match mode {
        Mode::Off => 0x00,
        Mode::Static => 0x01,
        Mode::Breathing => 0x02,
        Mode::Strobe => 0x03,
        Mode::ColorCycle => 0x04,
        Mode::Rainbow => 0x0E, // 0x05 is "random" on this chip
    }
}

pub struct Asrock {
    dev: Box<dyn HidTransport>,
    zones: Vec<Zone>,
    mode: Mode,
    speed: u8,
    correction: Correction,
}

impl Asrock {
    pub fn new(dev: Box<dyn HidTransport>) -> Result<Self> {
        let mut asrock = Asrock {
            dev,
            zones: Vec::new(),
            mode: Mode::Static,
            speed: SPEED_DEFAULT,
            correction: Correction::default(),
        };

        // LED count per zone, then the availability bitmask. The RGSwap
        // table is read in between, as the vendor tool does.
        let led_counts = asrock.read_header(CFG_LED_COUNT)?;
        asrock.read_header(CFG_RGSWAP)?;
        let available = asrock.read_header(CFG_ZONE_AVAIL)?;
        let zone_mask = available.get(0x04).copied().unwrap_or(0xFF);

        for zone in 0..8u8 {
            let count = led_counts.get(0x04 + usize::from(zone)).copied().unwrap_or(ZONE_UNAVAILABLE);
            if count != ZONE_UNAVAILABLE && (zone_mask >> zone) & 1 == 1 {
                asrock.zones.push(Zone::new(zone, ZONE_NAMES[usize::from(zone)]));
                log::debug!("Zone {zone} ({}): {count} LEDs", ZONE_NAMES[usize::from(zone)]);
            }
        }
        log::info!("ASRock Polychrome: {} zone(s) available", asrock.zones.len());

        // Colours are sent in the hardware's native G,R,B order, so the
        // board's own red/green swap has to be off or channels swap twice
        let mut buf = [0u8; REPORT_SIZE];
        buf[0x01] = CMD_WRITE_HEADER;
        buf[0x03] = CFG_RGSWAP;
        asrock.exchange(&buf)?;

        Ok(asrock)
    }

    /// Send a report and wait for the reply. An empty reply is a timeout.
    fn exchange(&mut self, buf: &[u8; REPORT_SIZE]) -> Result<Vec<u8>> {
        self.dev.write(buf)?;
        self.dev.read(64, 1000)
    }

    fn read_header(&mut self, table: u8) -> Result<Vec<u8>> {
        let mut buf = [0u8; REPORT_SIZE];
        buf[0x01] = CMD_READ_HEADER;
        buf[0x03] = table;
        self.exchange(&buf)
    }

    fn write_zone(&mut self, zone: u8, mode: Mode, [r, g, b]: Rgb) -> Result<()> {
        let mut buf = [0u8; REPORT_SIZE];
        buf[0x01] = CMD_SET_ZONE;
        buf[0x03] = zone;
        buf[0x04] = mode_value(mode);
        buf[0x05] = g;
        buf[0x06] = r;
        buf[0x07] = b;
        buf[0x08] = self.speed;
        buf[0x09] = 0xFF;
        self.exchange(&buf).map(drop)
    }
}

impl Controller for Asrock {
    fn name(&self) -> &str {
        "ASRock Polychrome USB"
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
        self.speed = speed.saturating_mul(51); // 0 -> 0x00, 5 -> 0xFF
        Ok(())
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        for zw in zones {
            if !self.zones.iter().any(|z| z.id == zw.zone) {
                continue;
            }
            // The mode is per zone, so a dark zone is one in "off" mode
            match zw.color {
                Some(color) => self.write_zone(zw.zone, self.mode, self.correction.apply(color))?,
                None => self.write_zone(zw.zone, Mode::Off, [0, 0, 0])?,
            }
        }
        let mut buf = [0u8; REPORT_SIZE];
        buf[0x01] = CMD_COMMIT;
        self.exchange(&buf).map(drop)
    }
}
