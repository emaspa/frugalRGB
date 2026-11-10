//! Creative Sound BlasterX Katana soundbar over USB HID (VID 0x041E).
//!
//! 64-byte frames `[0x5A magic, command, data length, data...]` on the
//! 64-byte HID interface (interface 4). Lighting is command 0x3A with
//! subcommands; state is stored per pattern *type* inside the currently
//! active device profile, so all writes edit (and persist to) that profile:
//!
//! ```text
//! 0x3A 0x10                                  get active profile number
//! 0x3A 0x08 <prof> <type>                    set active pattern type
//! 0x3A 0x04 0x00 <type> <ngroups> 0x00 <49 x slot>   LED -> palette-slot map
//! 0x3A 0x0A 0x00 <type> 0x01 <slot> <entries>        set palette entries
//! 0x3A 0x0A 0x00 <type> 0x03 <period LE16>           set speed (60000/BPM)
//! 0x3A 0x06 <0|1>                            lights off/on
//! ```
//!
//! Palette entries are 4 bytes: `[alpha, blue, green, red]`. The bar has 49
//! LEDs addressed through 7 groups of 7. Sets are acknowledged with an 0x02
//! frame carrying result code 0x00; gets are answered with an echo frame.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use super::Controller;
use crate::transport::HidTransport;
use crate::{Correction, Error, Mode, Result, Rgb, Zone, ZoneWrite};

pub const VID: u16 = 0x041E;
pub const PID: u16 = 0x3247;
pub const COMMAND_INTERFACE: i32 = 4;

const PACKET_SIZE: usize = 64;
const MAGIC: u8 = 0x5A;
const CMD_ERROR: u8 = 0x02;
const CMD_SYSINFO: u8 = 0x07;
const CMD_LIGHTING: u8 = 0x3A;

const SUB_SET_PATTERN: u8 = 0x04;
const SUB_ONOFF: u8 = 0x06;
const SUB_SET_TYPE: u8 = 0x08;
const SUB_SET_PARAM: u8 = 0x0A;
const SUB_CUR_PROFILE: u8 = 0x10;

const PARAM_PALETTE: u8 = 0x01;
const PARAM_SPEED: u8 = 0x03;

const TYPE_PULSATE: u8 = 0x01; // breathing
const TYPE_STATIC: u8 = 0x03; // solo (1 colour) / mood (7 zone colours)
const TYPE_WAVE: u8 = 0x04;
const TYPE_CYCLE: u8 = 0x05; // moving rainbow

const NUM_LEDS: usize = 49;
const NUM_GROUPS: usize = 7;

/// Speed 0 (fastest) to 5 (slowest) as effect period in ms.
const SPEED_PERIODS: [u16; 6] = [250, 500, 1000, 2500, 4000, 6000];

/// ROYGBIV in device `[alpha, blue, green, red]` order (factory rainbow).
const RAINBOW_PALETTE: [u8; 28] = [
    0xFF, 0x00, 0x00, 0xFF, // red
    0xFF, 0x00, 0x7F, 0xFF, // orange
    0xFF, 0x00, 0xFF, 0xFF, // yellow
    0xFF, 0x00, 0xFF, 0x00, // green
    0xFF, 0xFF, 0x00, 0x00, // blue
    0xFF, 0x82, 0x00, 0x4B, // indigo
    0xFF, 0xFF, 0x00, 0x8B, // violet
];

const MODES: [Mode; 5] = [
    Mode::Off,
    Mode::Static,
    Mode::Breathing,
    Mode::ColorCycle,
    Mode::Rainbow,
];

const REPLY_WINDOW: Duration = Duration::from_secs(1);
const READ_TIMEOUT_MS: i32 = 500;

/// Device state already written, so unchanged values are not sent again.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Written {
    Type,
    Pattern(u8),
    Palette(u8, u8),
    Speed(u8),
    Lights,
}

pub struct Katana {
    dev: Box<dyn HidTransport>,
    firmware: String,
    profile: u8,
    mode: Mode,
    speed: u8,
    correction: Correction,
    /// Colour per LED group; index 0 is zone 1.
    zone_colors: [Rgb; NUM_GROUPS],
    written: HashMap<Written, Vec<u8>>,
    zones: Vec<Zone>,
}

impl Katana {
    /// Query firmware and active profile. Returns `None` when `dev` is not
    /// the soundbar's command interface.
    pub fn probe(dev: Box<dyn HidTransport>) -> Result<Option<Self>> {
        let mut zones = vec![Zone::all(0, "All Zones")];
        zones.extend((1..=NUM_GROUPS as u8).map(|z| Zone::new(z, format!("Zone {z}"))));

        let mut katana = Katana {
            dev,
            firmware: String::new(),
            profile: 0,
            mode: Mode::Static,
            speed: 3,
            correction: Correction::default(),
            zone_colors: [[255, 255, 255]; NUM_GROUPS],
            written: HashMap::new(),
            zones,
        };

        // Firmware version: command 0x07, data 0x02, answered in ASCII
        katana.write_frame(CMD_SYSINFO, &[0x02])?;
        let deadline = Instant::now() + REPLY_WINDOW;
        while Instant::now() < deadline {
            let resp = katana.read_frame()?;
            if resp.len() > 3 && resp[1] == CMD_SYSINFO {
                let end = (3 + usize::from(resp[2])).min(resp.len());
                katana.firmware = resp[3..end]
                    .iter()
                    .take_while(|&&b| b != 0)
                    .map(|&b| if b.is_ascii() { b as char } else { char::REPLACEMENT_CHARACTER })
                    .collect();
                break;
            }
        }
        if katana.firmware.is_empty() {
            return Ok(None);
        }

        katana.profile = match katana.lighting_get(&[SUB_CUR_PROFILE])? {
            Some(reply) if reply.len() > 1 => reply[1],
            _ => 0,
        };
        log::info!(
            "Found Sound BlasterX Katana (fw {}, profile {})",
            katana.firmware,
            katana.profile
        );
        Ok(Some(katana))
    }

    pub fn firmware(&self) -> &str {
        &self.firmware
    }

    // -- Low-level protocol --

    fn write_frame(&mut self, cmd: u8, data: &[u8]) -> Result<()> {
        // Leading 0x00 is the (unnumbered) report ID; the frame is padded
        let mut buf = [0u8; PACKET_SIZE + 1];
        buf[1] = MAGIC;
        buf[2] = cmd;
        buf[3] = data.len() as u8;
        buf[4..4 + data.len()].copy_from_slice(data);
        self.dev
            .write(&buf)
            .map_err(|e| Error::Io(format!("Katana HID write failed: {e}")))
    }

    fn read_frame(&mut self) -> Result<Vec<u8>> {
        self.dev.read(PACKET_SIZE, READ_TIMEOUT_MS)
    }

    /// Send a lighting set command and wait for its 0x02 ack.
    fn lighting_set(&mut self, data: &[u8]) -> Result<()> {
        self.write_frame(CMD_LIGHTING, data)?;
        let sub = data[0];
        let deadline = Instant::now() + REPLY_WINDOW;
        while Instant::now() < deadline {
            let resp = self.read_frame()?;
            // Ack frame: 5a 02 <len> 3a <code> <sub> <echo...>
            if resp.len() > 5 && resp[1] == CMD_ERROR && resp[3] == CMD_LIGHTING && resp[5] == sub {
                if resp[4] != 0x00 {
                    return Err(Error::Protocol(format!(
                        "Katana rejected lighting sub 0x{sub:02X} (code 0x{:02X})",
                        resp[4]
                    )));
                }
                return Ok(());
            }
            // Anything else is an async event (volume, input, ...)
        }
        log::debug!("Katana: no ack for lighting sub 0x{sub:02X}");
        Ok(())
    }

    /// Send a lighting query and return the reply's data bytes, echo first.
    fn lighting_get(&mut self, data: &[u8]) -> Result<Option<Vec<u8>>> {
        self.write_frame(CMD_LIGHTING, data)?;
        let sub = data[0];
        let deadline = Instant::now() + REPLY_WINDOW;
        while Instant::now() < deadline {
            let resp = self.read_frame()?;
            if resp.len() > 3 && resp[1] == CMD_LIGHTING && resp[3] == sub {
                let end = (3 + usize::from(resp[2])).min(resp.len());
                return Ok(Some(resp[3..end].to_vec()));
            }
            if resp.len() > 5 && resp[1] == CMD_ERROR && resp[3] == CMD_LIGHTING && resp[5] == sub {
                return Ok(None);
            }
        }
        Ok(None)
    }

    // -- Cached device writes --

    fn set_if_changed(&mut self, key: Written, value: &[u8], command: &[u8]) -> Result<()> {
        if self.written.get(&key).map(Vec::as_slice) != Some(value) {
            self.lighting_set(command)?;
            self.written.insert(key, value.to_vec());
        }
        Ok(())
    }

    fn write_type(&mut self, ptype: u8) -> Result<()> {
        let command = [SUB_SET_TYPE, self.profile, ptype];
        self.set_if_changed(Written::Type, &[ptype], &command)
    }

    /// `groups`: palette slot per LED group, expanded to all 49 LEDs.
    fn write_pattern(&mut self, ptype: u8, groups: [u8; NUM_GROUPS]) -> Result<()> {
        let mut distinct = groups.to_vec();
        distinct.sort_unstable();
        distinct.dedup();

        let mut command = vec![SUB_SET_PATTERN, 0x00, ptype, distinct.len() as u8, 0x00];
        for slot in groups {
            command.extend(std::iter::repeat(slot).take(NUM_LEDS / NUM_GROUPS));
        }
        self.set_if_changed(Written::Pattern(ptype), &groups, &command)
    }

    fn write_palette(&mut self, ptype: u8, start_slot: u8, entries: &[u8], prefix: u8) -> Result<()> {
        let mut command = vec![SUB_SET_PARAM, prefix, ptype, PARAM_PALETTE, start_slot];
        command.extend_from_slice(entries);
        self.set_if_changed(Written::Palette(ptype, start_slot), entries, &command)
    }

    fn write_speed(&mut self, ptype: u8) -> Result<()> {
        let period = SPEED_PERIODS[usize::from(self.speed.min(5))].to_le_bytes();
        let command = [SUB_SET_PARAM, 0x00, ptype, PARAM_SPEED, period[0], period[1]];
        self.set_if_changed(Written::Speed(ptype), &period, &command)
    }

    fn write_lights(&mut self, on: bool) -> Result<()> {
        let on = u8::from(on);
        self.set_if_changed(Written::Lights, &[on], &[SUB_ONOFF, on])
    }

    fn commit(&mut self) -> Result<()> {
        let rainbow_groups = [1, 2, 3, 4, 5, 6, 7];
        match self.mode {
            Mode::Off => return self.write_lights(false),

            Mode::Static => {
                self.write_type(TYPE_STATIC)?;
                let colors = self.zone_colors;
                if colors.iter().all(|c| *c == colors[0]) {
                    // Solo: every LED on palette slot 1
                    self.write_pattern(TYPE_STATIC, [1; NUM_GROUPS])?;
                    self.write_palette(TYPE_STATIC, 1, &palette_entry(colors[0]), 0x00)?;
                } else {
                    // Mood: LED groups on slots 2-8, one colour each
                    self.write_pattern(TYPE_STATIC, [2, 3, 4, 5, 6, 7, 8])?;
                    let entries: Vec<u8> = colors.iter().flat_map(|c| palette_entry(*c)).collect();
                    self.write_palette(TYPE_STATIC, 2, &entries, 0x00)?;
                }
            }

            Mode::Breathing => {
                self.write_type(TYPE_PULSATE)?;
                self.write_speed(TYPE_PULSATE)?;
                // Pulsate only accepts palette writes with the 0x01 prefix.
                // If it refuses, the device keeps its stored pulsate colour.
                let entry = palette_entry(self.zone_colors[0]);
                match self.write_palette(TYPE_PULSATE, 1, &entry, 0x01) {
                    Err(e @ (Error::Io(_) | Error::Protocol(_))) => {
                        log::debug!("Katana: pulsate colour not accepted: {e}");
                    }
                    other => other?,
                }
            }

            Mode::ColorCycle => {
                self.write_type(TYPE_CYCLE)?;
                self.write_pattern(TYPE_CYCLE, rainbow_groups)?;
                self.write_palette(TYPE_CYCLE, 1, &RAINBOW_PALETTE, 0x00)?;
                self.write_speed(TYPE_CYCLE)?;
            }

            Mode::Rainbow => {
                self.write_type(TYPE_WAVE)?;
                self.write_pattern(TYPE_WAVE, rainbow_groups)?;
                self.write_palette(TYPE_WAVE, 1, &RAINBOW_PALETTE, 0x00)?;
                self.write_speed(TYPE_WAVE)?;
            }

            // No hardware strobe; the lights are simply kept on
            Mode::Strobe => {}
        }
        self.write_lights(true)
    }
}

fn palette_entry([r, g, b]: Rgb) -> [u8; 4] {
    [0xFF, b, g, r]
}

impl Controller for Katana {
    fn name(&self) -> &str {
        "Sound BlasterX Katana"
    }

    fn zones(&self) -> &[Zone] {
        &self.zones
    }

    fn supported_modes(&self) -> &'static [Mode] {
        &MODES
    }

    fn hardware_animates(&self, mode: Mode) -> bool {
        MODES.contains(&mode)
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
        for zw in zones {
            // The effect is global here, so a dark zone is a black one
            let color = self.correction.apply(zw.color.unwrap_or([0, 0, 0]));
            match usize::from(zw.zone) {
                0 => self.zone_colors = [color; NUM_GROUPS],
                z if z <= NUM_GROUPS => self.zone_colors[z - 1] = color,
                _ => {}
            }
        }
        self.commit()
    }
}
