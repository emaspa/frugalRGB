//! Gigabyte RGB Fusion 2.0 over USB HID (ITE IT5711 / IT8297 family).
//!
//! Every command is a 64-byte feature report on report ID 0xCC. Each LED has
//! an effect register (0x20-0x27, plus 0x90-0x92 on IT5711); writes take
//! effect when an apply report (0x28) names their bits in its mask.

use std::thread::sleep;
use std::time::Duration;

use super::{hex_rows, Controller};
use crate::transport::HidTransport;
use crate::{Correction, Error, Mode, Result, Rgb, Zone, ZoneWrite};

pub const VID: u16 = 0x048D;
pub const PID_IT8297: u16 = 0x8297;
pub const PID_IT8950: u16 = 0x8950;
pub const PID_IT5702: u16 = 0x5702;
pub const PID_IT5711: u16 = 0x5711;
pub const KNOWN_PIDS: [u16; 4] = [PID_IT8297, PID_IT8950, PID_IT5702, PID_IT5711];

/// HID usage the RGB collection is published under.
pub const USAGE_PAGE: u16 = 0xFF89;
pub const USAGE: u16 = 0xCC;

const REPORT_ID: u8 = 0xCC;
const PACKET_SIZE: usize = 64;

const EFFECT_NONE: u8 = 0;
const EFFECT_STATIC: u8 = 1;
const EFFECT_PULSE: u8 = 2;
const EFFECT_BLINKING: u8 = 3;
const EFFECT_COLORCYCLE: u8 = 4;

const MODES: [Mode; 6] = [
    Mode::Off,
    Mode::Static,
    Mode::Breathing,
    Mode::Strobe,
    Mode::ColorCycle,
    Mode::Rainbow,
];

/// LED index and zone name. X870E Aorus Master / Pro / ICE (IT5711).
const LAYOUT_IT5711: &[(u8, &str)] = &[
    (4, "12V RGB Header"),
    (5, "D_LED1"),
    (6, "D_LED2"),
    (7, "D_LED3"),
    (8, "D_LED4"),
    (9, "Logo"),
    (10, "Accent"),
];

/// Older boards on IT8297-class chips (Z390, X570, B550, ...).
const LAYOUT_IT8297: &[(u8, &str)] = &[
    (0, "Back I/O"),
    (1, "CPU Header"),
    (2, "PCIe"),
    (3, "LED C1/C2"),
    (4, "12V RGB Header"),
    (5, "D_LED1"),
    (6, "D_LED2"),
];

fn effect_for(mode: Mode) -> u8 {
    match mode {
        Mode::Off => EFFECT_NONE,
        Mode::Static => EFFECT_STATIC,
        Mode::Breathing => EFFECT_PULSE,
        Mode::Strobe => EFFECT_BLINKING,
        // Rainbow is colour cycle with the random-colour flag set
        Mode::ColorCycle | Mode::Rainbow => EFFECT_COLORCYCLE,
    }
}

/// Effect register of one LED: 0x20-0x27, then 0x90-0x92 on IT5711.
fn register(led: u8) -> u8 {
    match led {
        0..=7 => 0x20 + led,
        8..=10 => 0x90 + (led - 8),
        _ => 0x20,
    }
}

/// Hardware speed step (0-9) to breathing period in ms.
fn breathing_period(speed: u16) -> u16 {
    if speed <= 6 {
        400 + speed * 100
    } else {
        1000 + (speed - 6) * 200
    }
}

fn blinking_period(speed: u16) -> u16 {
    speed * 200 + 700
}

fn colorcycle_period(speed: u16) -> u16 {
    let period = speed * 100 + 300;
    if speed > 8 {
        period + 1300 * (speed - 8)
    } else {
        period
    }
}

/// What the 0x60 info report says about the chip.
struct DeviceInfo {
    product: String,
    firmware: String,
    chip_id: u32,
}

fn command(dev: &mut dyn HidTransport, a: u8, b: u8, c: u8) -> Result<()> {
    let mut pkt = [0u8; PACKET_SIZE];
    pkt[0] = REPORT_ID;
    pkt[1] = a;
    pkt[2] = b;
    pkt[3] = c;
    dev.send_feature_report(&pkt)
}

fn read_info(dev: &mut dyn HidTransport) -> Result<DeviceInfo> {
    command(dev, 0x60, 0, 0)?;
    let info = dev.get_feature_report(REPORT_ID, PACKET_SIZE)?;

    let product = if info.len() >= 40 {
        info[12..40]
            .iter()
            .take_while(|&&b| b != 0)
            .map(|&b| if b.is_ascii() { b as char } else { char::REPLACEMENT_CHARACTER })
            .collect()
    } else {
        String::new()
    };
    let firmware = if info.len() >= 8 {
        info[4..8].iter().map(u8::to_string).collect::<Vec<_>>().join(".")
    } else {
        String::new()
    };
    let chip_id = if info.len() >= 60 {
        u32::from_le_bytes([info[56], info[57], info[58], info[59]])
    } else {
        0
    };
    Ok(DeviceInfo { product, firmware, chip_id })
}

pub struct Gigabyte {
    dev: Box<dyn HidTransport>,
    pid: u16,
    chip_id: u32,
    is_5711: bool,
    /// Whether [`take_over`](Self::take_over) has run.
    taken_over: bool,
    name: String,
    firmware: String,
    zones: Vec<Zone>,
    /// LED index behind each zone; `None` addresses every LED at once.
    leds: Vec<Option<u8>>,
    mode: Mode,
    speed: u8,
    correction: Correction,
}

impl Gigabyte {
    /// Check that `dev` answers the info request with a product string, the
    /// way the RGB interface of the chip does and its other interfaces don't.
    pub fn responds(dev: &mut dyn HidTransport) -> bool {
        matches!(read_info(dev), Ok(info) if !info.product.is_empty())
    }

    /// Read the controller's identity. Nothing is changed on the device
    /// until the first write, so listing devices leaves the lighting alone.
    pub fn new(mut dev: Box<dyn HidTransport>, pid: u16) -> Result<Self> {
        // IT5702 is an IT8297-class chip: only 0x5711 has the three extra
        // LED registers and the second calibration report
        let is_5711 = pid == PID_IT5711;

        let info = read_info(dev.as_mut())?;
        log::info!(
            "Gigabyte RGB: product={} fw={} chip=0x{:08X}",
            info.product,
            info.firmware,
            info.chip_id
        );

        if is_5711 {
            command(dev.as_mut(), 0x61, 0, 0)?;
            dev.get_feature_report(REPORT_ID, PACKET_SIZE)?;
        }

        let layout = if is_5711 { LAYOUT_IT5711 } else { LAYOUT_IT8297 };
        let mut zones = vec![Zone::all(0, "All Zones")];
        let mut leds = vec![None];
        for &(led, zone_name) in layout {
            zones.push(Zone::new(zones.len() as u8, zone_name));
            leds.push(Some(led));
        }

        let name = if info.product.is_empty() {
            "Gigabyte RGB Fusion 2.0".to_string()
        } else {
            format!("Gigabyte RGB ({})", info.product)
        };

        Ok(Gigabyte {
            dev,
            pid,
            chip_id: info.chip_id,
            is_5711,
            taken_over: false,
            name,
            firmware: info.firmware,
            zones,
            leds,
            mode: Mode::Static,
            speed: 3,
            correction: Correction::default(),
        })
    }

    pub fn firmware(&self) -> &str {
        &self.firmware
    }

    /// Before the first write, put the controller in a known state: out of
    /// Lamp Array mode, every effect register cleared, beat mode off.
    fn take_over(&mut self) -> Result<()> {
        if self.taken_over {
            return Ok(());
        }
        let dev = self.dev.as_mut();

        // Leave Lamp Array mode, or Windows 11 Dynamic Lighting overrides us
        command(dev, 0x48, 0x00, 0)?;

        // Reset all effect registers, then fast-apply
        for reg in 0x20..0x28 {
            command(dev, reg, 0, 0)?;
        }
        if self.is_5711 {
            for reg in 0x90..0x93 {
                command(dev, reg, 0, 0)?;
            }
        }
        command(dev, 0x28, 0xFF, if self.is_5711 { 0x07 } else { 0x00 })?;

        // Beat (audio) mode off; built-in effects enabled on all ARGB headers
        command(dev, 0x31, 0x00, 0)?;
        command(dev, 0x32, 0x00, 0)?;

        self.taken_over = true;
        Ok(())
    }

    /// Build the effect report for one LED (or all of them) and return it
    /// with the apply-mask bits it covers.
    fn effect_packet(&self, led: Option<u8>, mode: Mode, color: Rgb) -> ([u8; PACKET_SIZE], u32) {
        let mut pkt = [0u8; PACKET_SIZE];
        pkt[0] = REPORT_ID;

        let mask: u32 = match led {
            None => {
                pkt[1] = 0x20;
                if self.is_5711 {
                    0x07FF
                } else {
                    0xFF
                }
            }
            Some(led) => {
                pkt[1] = register(led);
                1 << led
            }
        };
        pkt[2..6].copy_from_slice(&mask.to_le_bytes());

        let effect = effect_for(mode);
        pkt[11] = effect;
        // Max brightness, capped for breathing because of a hardware quirk
        pkt[12] = if effect == EFFECT_PULSE { 100 } else { 0xFF };

        // Colour as a little-endian 0x00RRGGBB
        let [r, g, b] = color;
        pkt[14] = b;
        pkt[15] = g;
        pkt[16] = r;

        let hw_speed = u16::from(self.speed).saturating_mul(2).min(9);
        match mode {
            Mode::Breathing => pkt[22..24].copy_from_slice(&breathing_period(hw_speed).to_le_bytes()),
            Mode::Strobe => pkt[26..28].copy_from_slice(&blinking_period(hw_speed).to_le_bytes()),
            Mode::ColorCycle | Mode::Rainbow => {
                pkt[22..24].copy_from_slice(&colorcycle_period(hw_speed).to_le_bytes())
            }
            Mode::Off | Mode::Static => {}
        }
        if mode == Mode::Rainbow {
            pkt[30] = 7;
        }

        (pkt, mask)
    }
}

impl Controller for Gigabyte {
    fn facts(&self) -> Vec<(&'static str, String)> {
        vec![
            ("PID", format!("0x{:04X}", self.pid)),
            ("Firmware", self.firmware.clone()),
            ("Chip ID", format!("0x{:08X}", self.chip_id)),
            ("IT5711 registers", self.is_5711.to_string()),
        ]
    }

    fn dump(&mut self) -> Option<(String, String)> {
        let mut text = String::new();
        let dev = self.dev.as_mut();
        match command(dev, 0x60, 0, 0).and_then(|()| dev.get_feature_report(REPORT_ID, PACKET_SIZE)) {
            Ok(info) => {
                text.push_str(&format!("Device info report ({} bytes)\n{}", info.len(), hex_rows(&info, 0)));
                if info.len() >= 12 {
                    text.push_str(&format!(
                        "  product byte: 0x{:02X}\n  device_num: {}\n  strip_detect: 0x{:02X}\n  \
                         led_count_high: 0x{:02X}\n  led_count_low: 0x{:02X}\n  strip_ctrl_length1: {}\n  \
                         support_cmd_flag: 0x{:02X}\n",
                        info[1], info[2], info[3], info[8], info[9], info[10], info[11]
                    ));
                }
            }
            Err(e) => text.push_str(&format!("Failed to read the device info report: {e}\n")),
        }
        text.push_str("Zone layout\n");
        for (zone, led) in self.zones.iter().zip(&self.leds) {
            let led = led.map_or("all".to_string(), |led| led.to_string());
            text.push_str(&format!("  Zone {}: {} (LED index {led})\n", zone.id, zone.name));
        }
        Some((format!("Gigabyte RGB Fusion 2.0 (PID 0x{:04X})", self.pid), text))
    }

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
        self.speed = speed;
        Ok(())
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        self.take_over()?;
        let mut touched = 0u32;
        for zw in zones {
            let Some(&led) = self.leds.get(usize::from(zw.zone)) else {
                continue;
            };
            // The effect is per LED, so a dark zone gets the "none" effect
            // and stays dark while its neighbours cycle colours
            let (pkt, mask) = match zw.color {
                Some(color) => self.effect_packet(led, self.mode, self.correction.apply(color)),
                None => self.effect_packet(led, Mode::Off, [0, 0, 0]),
            };
            self.dev.send_feature_report(&pkt)?;
            touched |= mask;
        }

        let mut pkt = [0u8; PACKET_SIZE];
        pkt[0] = REPORT_ID;
        pkt[1] = 0x28;
        pkt[2..6].copy_from_slice(&touched.to_le_bytes());
        self.dev.send_feature_report(&pkt)
    }

    fn supports_save(&self) -> bool {
        true
    }

    fn save(&mut self) -> Result<()> {
        // Enable persistence, then write the current state to flash. Without
        // the 0x5E write the board keeps booting with its old stored colours.
        command(self.dev.as_mut(), 0x47, 0x01, 0)?;
        sleep(Duration::from_millis(20));
        command(self.dev.as_mut(), 0x5E, 0x00, 0)
            .map_err(|e| Error::Io(format!("{}: flash save failed: {e}", self.name)))
    }
}
