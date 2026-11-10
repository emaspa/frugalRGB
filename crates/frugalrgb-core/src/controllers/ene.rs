//! ENE RGB controllers on SMBus / I2C: DDR5 and DDR4 memory, and the ENE
//! chip on ASUS graphics cards.
//!
//! Registers are 16-bit. A word write to command 0x00 selects the register
//! (address byte-swapped); the value is then written through command 0x01
//! or read back through command 0x81.

use std::sync::{Arc, Mutex, MutexGuard};

use super::Controller;
use crate::transport::SmBus;
use crate::{Correction, Error, Mode, Result, Rgb, Zone, ZoneWrite};

/// A bus shared by several devices, as the DIMMs on the chipset SMBus are.
pub type SharedBus = Arc<Mutex<dyn SmBus>>;

const REG_DEVICE_NAME: u16 = 0x1000;
const REG_MICRON_CHECK: u16 = 0x1030;
const REG_CONFIG_TABLE: u16 = 0x1C00;
const REG_DIRECT_COLOR: u16 = 0x8000;
const REG_DIRECT_SELECT: u16 = 0x8020; // 0x00 = effect mode, 0x01 = direct
const REG_MODE: u16 = 0x8021;
const REG_SPEED: u16 = 0x8022;
const REG_DIRECTION: u16 = 0x8023;
const REG_V2_EFFECT_COLOR: u16 = 0x8160; // 3 bytes per LED
const REG_APPLY: u16 = 0x80A0;

const APPLY: u8 = 0x01;
const SAVE: u8 = 0xAA;

/// DIMM RGB controllers answer on these addresses (slot = address - 0x70).
const DIMM_ADDRS: std::ops::Range<u8> = 0x70..0x78;
/// The ENE chip on ASUS graphics cards (RTX 30 series and later).
pub const GPU_ADDR: u8 = 0x67;

const MODES: [Mode; 6] = [
    Mode::Off,
    Mode::Static,
    Mode::Breathing,
    Mode::ColorCycle,
    Mode::Rainbow,
    Mode::Strobe,
];

fn mode_value(mode: Mode) -> u8 {
    match mode {
        Mode::Off => 0x00,
        Mode::Static => 0x01,
        Mode::Breathing => 0x02,
        Mode::Strobe => 0x03,
        Mode::ColorCycle => 0x04,
        Mode::Rainbow => 0x05,
    }
}

/// Speed 0 (fastest) to 5: the chip has five steps, 0x00 fast to 0x04 slow.
fn speed_value(speed: u8) -> u8 {
    match speed {
        0..=4 => speed,
        5 => 0x04,
        _ => 0x03,
    }
}

fn lock(bus: &SharedBus) -> Result<MutexGuard<'_, dyn SmBus + 'static>> {
    bus.lock().map_err(|_| Error::Io("SMBus is unusable after an earlier failure".into()))
}

fn select(bus: &mut dyn SmBus, addr: u8, reg: u16) -> Result<()> {
    bus.write_word_data(addr, 0x00, reg.swap_bytes())
}

fn read_register(bus: &mut dyn SmBus, addr: u8, reg: u16) -> Result<u8> {
    select(bus, addr, reg)?;
    bus.read_byte_data(addr, 0x81)
}

fn write_register(bus: &mut dyn SmBus, addr: u8, reg: u16, value: u8) -> Result<()> {
    select(bus, addr, reg)?;
    bus.write_byte_data(addr, 0x01, value)
}

/// Read the chip's name string. `None` when nothing ENE-like answers.
pub fn read_name(bus: &mut dyn SmBus, addr: u8) -> Option<String> {
    let mut name = String::new();
    for i in 0..16 {
        match read_register(bus, addr, REG_DEVICE_NAME + i).ok()? {
            0 => break,
            c @ 0x20..=0x7E => name.push(c as char),
            _ => return None, // not text, so not an ENE chip
        }
    }
    (name.len() >= 4).then_some(name)
}

/// LED count from the config table, if it holds a plausible one.
fn led_count(bus: &mut dyn SmBus, addr: u8, offset: u16) -> Option<u8> {
    read_register(bus, addr, REG_CONFIG_TABLE + offset).ok().filter(|n| (1..=32).contains(n))
}

/// An ENE chip with per-LED effect colours: DDR5 DIMMs and ASUS GPUs.
pub struct EneV2 {
    bus: SharedBus,
    addr: u8,
    name: String,
    num_leds: u8,
    direction: u8,
    zones: Vec<Zone>,
    correction: Correction,
}

impl EneV2 {
    /// A DDR5 DIMM at `addr`. `direction` flips the LED order of animated
    /// effects so that neighbouring sticks run the same way.
    fn ddr5(bus: SharedBus, addr: u8, direction: u8) -> Result<Self> {
        let num_leds = led_count(&mut *lock(&bus)?, addr, 2).unwrap_or(8);
        let mut zones = vec![Zone::all(0, "All LEDs")];
        zones.extend((1..=num_leds).map(|i| Zone::new(i, format!("LED {i}"))));
        Self::start(bus, addr, format!("ENE DDR5 (0x{addr:02X})"), num_leds, direction, zones)
    }

    /// The ENE chip of an ASUS graphics card. `label` names the card.
    pub fn gpu(bus: SharedBus, label: &str) -> Result<Self> {
        let num_leds = led_count(&mut *lock(&bus)?, GPU_ADDR, 3).unwrap_or(1);
        let zones = if num_leds == 1 {
            vec![Zone::new(0, "GPU")]
        } else {
            let mut zones = vec![Zone::all(0, "All LEDs")];
            zones.extend((1..=num_leds).map(|i| Zone::new(i, format!("LED {i}"))));
            zones
        };
        log::info!("ASUS GPU RGB: {num_leds} LED(s)");
        Self::start(bus, GPU_ADDR, label.to_string(), num_leds, 0x00, zones)
    }

    fn start(
        bus: SharedBus,
        addr: u8,
        name: String,
        num_leds: u8,
        direction: u8,
        zones: Vec<Zone>,
    ) -> Result<Self> {
        // Effect mode: the V2 effect colour registers are only used in it
        write_register(&mut *lock(&bus)?, addr, REG_DIRECT_SELECT, 0x00)?;
        Ok(EneV2 { bus, addr, name, num_leds, direction, zones, correction: Correction::default() })
    }

    /// Colour registers hold red, blue, green in that order.
    fn write_led(bus: &mut dyn SmBus, addr: u8, led: u8, [r, g, b]: Rgb) -> Result<()> {
        let base = REG_V2_EFFECT_COLOR + u16::from(led) * 3;
        write_register(bus, addr, base, r)?;
        write_register(bus, addr, base + 1, b)?;
        write_register(bus, addr, base + 2, g)
    }
}

impl Controller for EneV2 {
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
        let mut bus = lock(&self.bus)?;
        write_register(&mut *bus, self.addr, REG_DIRECT_SELECT, 0x00)?;
        write_register(&mut *bus, self.addr, REG_MODE, mode_value(mode))?;
        write_register(&mut *bus, self.addr, REG_SPEED, speed_value(speed))?;
        write_register(&mut *bus, self.addr, REG_DIRECTION, self.direction)
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        let mut bus = lock(&self.bus)?;
        for zw in zones {
            // The mode is global to the chip, so a dark LED is a black one
            let color = self.correction.apply(zw.color.unwrap_or([0, 0, 0]));
            match zw.zone {
                0 => {
                    for led in 0..self.num_leds {
                        Self::write_led(&mut *bus, self.addr, led, color)?;
                    }
                }
                zone if zone <= self.num_leds => {
                    Self::write_led(&mut *bus, self.addr, zone - 1, color)?;
                }
                _ => {}
            }
        }
        write_register(&mut *bus, self.addr, REG_APPLY, APPLY)
    }

    fn supports_save(&self) -> bool {
        true
    }

    fn save(&mut self) -> Result<()> {
        write_register(&mut *lock(&self.bus)?, self.addr, REG_APPLY, SAVE)
    }
}

/// Find ENE AUDA-series DDR5 DIMMs on the chipset SMBus.
pub fn detect_ddr5(bus: &SharedBus) -> Vec<(u8, EneV2)> {
    let mut found = Vec::new();
    for addr in DIMM_ADDRS {
        let Ok(mut guard) = lock(bus) else { break };
        let Some(name) = read_name(&mut *guard, addr) else {
            continue;
        };
        drop(guard);
        if !name.starts_with("AUD") {
            log::debug!("Unknown device at 0x{addr:02X}: {name}");
            continue;
        }

        // Sticks in odd slots are mounted with the LED order reversed
        let slot = addr - 0x70;
        let direction = slot % 2;
        log::info!("Found ENE DDR5 controller at 0x{addr:02X}: {name} (slot {slot}, dir={direction})");
        match EneV2::ddr5(Arc::clone(bus), addr, direction) {
            Ok(ctrl) => found.push((addr, ctrl)),
            Err(e) => log::debug!("ENE DDR5 at 0x{addr:02X} rejected: {e}"),
        }
    }
    found
}

/// A classic ENE DRAM controller (DDR4): one zone, direct colour block.
pub struct EneDram {
    bus: SharedBus,
    addr: u8,
    name: String,
    zones: Vec<Zone>,
}

impl EneDram {
    const NUM_LEDS: usize = 8;

    fn new(bus: SharedBus, addr: u8) -> Self {
        EneDram {
            bus,
            addr,
            name: format!("ENE DRAM (0x{addr:02X})"),
            zones: vec![Zone::new(0, "DRAM")],
        }
    }
}

impl Controller for EneDram {
    fn name(&self) -> &str {
        &self.name
    }

    fn zones(&self) -> &[Zone] {
        &self.zones
    }

    fn supported_modes(&self) -> &'static [Mode] {
        &MODES
    }

    fn hardware_animates(&self, _mode: Mode) -> bool {
        false
    }

    fn set_correction(&mut self, _correction: Correction) {
        // These sticks have never been calibrated; colours go out as given
    }

    fn set_mode(&mut self, mode: Mode, speed: u8) -> Result<()> {
        let mut bus = lock(&self.bus)?;
        write_register(&mut *bus, self.addr, REG_DIRECT_SELECT, 0x00)?;
        write_register(&mut *bus, self.addr, REG_MODE, mode_value(mode))?;
        write_register(&mut *bus, self.addr, REG_SPEED, speed_value(speed))
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        let mut bus = lock(&self.bus)?;
        for zw in zones {
            let [r, g, b] = zw.color.unwrap_or([0, 0, 0]);
            // Every LED gets the colour: select the block once, then the
            // register pointer advances with each byte (red, blue, green)
            select(&mut *bus, self.addr, REG_DIRECT_COLOR)?;
            for _ in 0..Self::NUM_LEDS {
                for byte in [r, b, g] {
                    bus.write_byte_data(self.addr, 0x01, byte)?;
                }
            }
        }
        write_register(&mut *bus, self.addr, REG_APPLY, APPLY)
    }
}

/// Find classic ENE DRAM controllers, skipping the addresses in `claimed`.
///
/// A device qualifies when registers 0xA0-0xAF read back 0x00-0x0F and it
/// is not a Micron DIMM, which sits on the same addresses.
pub fn detect_dram(bus: &SharedBus, claimed: &[u8]) -> Vec<EneDram> {
    let mut found = Vec::new();
    for addr in DIMM_ADDRS.filter(|a| !claimed.contains(a)) {
        let Ok(mut guard) = lock(bus) else { break };
        let bus_ref = &mut *guard;

        let is_ene = (|| -> Result<bool> {
            // Does anything answer at all?
            read_register(bus_ref, addr, 0x00A0)?;
            for i in 0..16u16 {
                if u16::from(read_register(bus_ref, addr, 0x00A0 + i)?) != i {
                    return Ok(false);
                }
            }
            select(bus_ref, addr, REG_MICRON_CHECK)?;
            let mut vendor = [0u8; 6];
            for (i, byte) in vendor.iter_mut().enumerate() {
                *byte = read_register(bus_ref, addr, REG_MICRON_CHECK + i as u16)?;
            }
            if &vendor == b"Micron" {
                log::info!("Skipping Micron DRAM at 0x{addr:02X}");
                return Ok(false);
            }
            Ok(true)
        })();

        if matches!(is_ene, Ok(true)) {
            log::info!("Found ENE DRAM controller at 0x{addr:02X}");
            found.push(EneDram::new(Arc::clone(bus), addr));
        }
    }
    found
}
