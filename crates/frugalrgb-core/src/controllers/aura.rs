//! ASUS Aura mainboard RGB over USB HID (VID 0x0B05).
//!
//! Follows OpenRGB's `AuraMainboardController`, used by most ASUS boards
//! from X570/B550 onwards.
//!
//! * The device takes HID *output* reports, not feature reports.
//! * Every packet is 65 bytes and starts with 0xEC, which doubles as the
//!   report ID. The command byte follows it.
//! * Zones are not fixed: onboard LEDs, RGB headers and addressable headers
//!   come from a 60-byte config table the device reports.
//! * Onboard ("fixed") lighting uses the effect (0x35) and effect-colour
//!   (0x36) commands; addressable headers use the direct (0x40) command.

use super::Controller;
use crate::transport::HidTransport;
use crate::{Correction, Mode, Result, Rgb, Zone, ZoneWrite};

pub const VID: u16 = 0x0B05;
/// The controller on recent boards such as the B760M BTF.
pub const KNOWN_PIDS: [u16; 1] = [0x19AF];
/// Reported on older boards but not verified against this implementation.
pub const EXPERIMENTAL_PIDS: [u16; 2] = [0x1939, 0x18F3];

const REPORT_SIZE: usize = 65;
const PREFIX: u8 = 0xEC;

const REQUEST_FIRMWARE: u8 = 0x82;
const REQUEST_CONFIG: u8 = 0xB0;
const REPLY_FIRMWARE: u8 = 0x02;
const REPLY_CONFIG: u8 = 0x30;

const CMD_EFFECT: u8 = 0x35;
const CMD_EFFECT_COLOR: u8 = 0x36;
const CMD_COMMIT: u8 = 0x3F;
const CMD_DIRECT: u8 = 0x40;
const GEN1_HANDSHAKE: [u8; 4] = [0x52, 0x53, 0x00, 0x01];

const MODE_OFF: u8 = 0x00;
const MODE_STATIC: u8 = 0x01;
const MODE_DIRECT: u8 = 0xFF;

const LEDS_PER_PACKET: usize = 0x14;
/// Effect-colour packets carry a 16-bit LED mask.
const MAX_ONBOARD_LEDS: usize = 16;
/// Addressable headers don't report their strip length. Fill this many
/// LEDs; those beyond the physical strip are ignored.
const DEFAULT_ADDRESSABLE_LEDS: usize = 60;

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
        Mode::Off => MODE_OFF,
        Mode::Static => MODE_STATIC,
        Mode::Breathing => 0x02,
        Mode::Strobe => 0x03,
        Mode::ColorCycle => 0x04,
        Mode::Rainbow => 0x05,
    }
}

/// Breathing and strobe are colour effects the firmware can only run on the
/// onboard LEDs, not on addressable headers (OpenRGB has the same limit), so
/// they are streamed as direct-mode frames by the software loop instead.
fn software_animated(mode: Mode) -> bool {
    matches!(mode, Mode::Breathing | Mode::Strobe)
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    Fixed,
    Addressable,
}

/// One lighting channel from the config table.
struct Channel {
    kind: Kind,
    /// Channel number in effect packets.
    effect: u8,
    /// Device number in direct packets.
    direct: u8,
    num_leds: usize,
    start_led: usize,
}

/// Colours by channel index, in the order the channels were first written.
#[derive(Default)]
struct ChannelColors(Vec<(usize, Rgb)>);

impl ChannelColors {
    fn set(&mut self, channel: usize, color: Rgb) {
        match self.0.iter_mut().find(|(c, _)| *c == channel) {
            Some(entry) => entry.1 = color,
            None => self.0.push((channel, color)),
        }
    }
}

pub struct Aura {
    dev: Box<dyn HidTransport>,
    name: String,
    firmware: String,
    config_ok: bool,
    channels: Vec<Channel>,
    zones: Vec<Zone>,
    mode: Mode,
    correction: Correction,
    pending: ChannelColors,
    /// Last colours committed to the device, for saving to hardware.
    applied: ChannelColors,
    /// Whether the channels are already in direct mode for the software
    /// effect running now, so that frames only need to stream LED data.
    direct_armed: bool,
}

impl Aura {
    pub fn new(dev: Box<dyn HidTransport>, pid: u16, product: &str) -> Result<Self> {
        let product = product.trim();
        let name = if product.is_empty() {
            format!("ASUS Aura USB (0x{pid:04X})")
        } else {
            format!("ASUS Aura USB ({product})")
        };

        let mut aura = Aura {
            dev,
            name,
            firmware: String::new(),
            config_ok: false,
            channels: Vec::new(),
            zones: Vec::new(),
            mode: Mode::Static,
            correction: Correction::default(),
            pending: ChannelColors::default(),
            applied: ChannelColors::default(),
            direct_armed: false,
        };

        aura.firmware = aura.read_firmware()?;
        let config = aura.read_config_table(pid)?;
        aura.config_ok = config.is_some();
        aura.build_channels(config.as_deref());
        aura.write(&GEN1_HANDSHAKE)?;

        // Zone 0 is the all-zones master; zones 1..N map to channels
        aura.zones.push(Zone::all(0, "All Zones"));
        let mut header = 0;
        for (i, channel) in aura.channels.iter().enumerate() {
            let zone_name = match channel.kind {
                Kind::Fixed => "Onboard RGB".to_string(),
                Kind::Addressable => {
                    header += 1;
                    format!("Addressable Header {header}")
                }
            };
            aura.zones.push(Zone::new(i as u8 + 1, zone_name));
        }
        Ok(aura)
    }

    pub fn firmware(&self) -> &str {
        &self.firmware
    }

    /// False when the device did not report its config table and a single
    /// onboard zone was assumed. Another interface may do better.
    pub fn config_ok(&self) -> bool {
        self.config_ok
    }

    // -- Low-level HID --

    /// Send a 65-byte output report: 0xEC, then `data`, zero padded.
    fn write(&mut self, data: &[u8]) -> Result<()> {
        let mut packet = [0u8; REPORT_SIZE];
        packet[0] = PREFIX;
        let n = data.len().min(REPORT_SIZE - 1);
        packet[1..1 + n].copy_from_slice(&data[..n]);
        self.dev.write(&packet)
    }

    /// Send a request and return up to `length` payload bytes of the reply.
    ///
    /// The reply is `[0xEC, reply_id, ...]` with the payload `data_offset`
    /// bytes after the reply id. Some backends strip the leading report ID,
    /// so the reply id is looked for at index 1, then 0.
    fn query(&mut self, request: u8, reply_id: u8, data_offset: usize, length: usize) -> Result<Option<Vec<u8>>> {
        self.write(&[request])?;
        let resp = match self.dev.read(REPORT_SIZE, 1000) {
            Ok(resp) => resp,
            Err(e) => {
                log::debug!("ASUS Aura read failed (request 0x{request:02X}): {e}");
                return Ok(None);
            }
        };
        for idx in [1, 0] {
            if resp.get(idx) == Some(&reply_id) {
                let start = (idx + data_offset).min(resp.len());
                let end = (start + length).min(resp.len());
                return Ok(Some(resp[start..end].to_vec()));
            }
        }
        if !resp.is_empty() {
            log::debug!("ASUS Aura unexpected reply for request 0x{request:02X}");
        }
        Ok(None)
    }

    fn read_firmware(&mut self) -> Result<String> {
        let payload = self.query(REQUEST_FIRMWARE, REPLY_FIRMWARE, 1, 16)?.unwrap_or_default();
        let text: String = payload
            .iter()
            .take_while(|&&b| b != 0)
            .map(|&b| if b.is_ascii() { b as char } else { char::REPLACEMENT_CHARACTER })
            .collect();
        let text = text.trim();
        Ok(if text.is_empty() { "unknown".to_string() } else { text.to_string() })
    }

    fn read_config_table(&mut self, pid: u16) -> Result<Option<Vec<u8>>> {
        match self.query(REQUEST_CONFIG, REPLY_CONFIG, 3, 60)? {
            Some(config) if config.len() >= 0x1E => Ok(Some(config)),
            _ => {
                log::warn!(
                    "ASUS Aura: could not read config table (PID=0x{pid:04X}), falling back to \
                     a single onboard zone. Is Armoury Crate / ASUS LightingService still running?"
                );
                Ok(None)
            }
        }
    }

    /// Derive the channels from the config table: byte 0x1B is the number
    /// of onboard LEDs, 0x1D the RGB headers among them, 0x02 the number of
    /// addressable headers.
    fn build_channels(&mut self, config: Option<&[u8]>) {
        let fallback = || Channel { kind: Kind::Fixed, effect: 0, direct: 0x04, num_leds: 1, start_led: 0 };
        let Some(config) = config else {
            self.channels.push(fallback());
            return;
        };

        let onboard_leds = usize::from(config[0x1B]);
        let rgb_headers = if onboard_leds < usize::from(config[0x1D]) { 0 } else { config[0x1D] };
        let addressable = config[0x02];

        let mut effect = 0u8;
        if onboard_leds > 0 {
            self.channels.push(Channel {
                kind: Kind::Fixed,
                effect,
                direct: 0x04,
                num_leds: onboard_leds,
                start_led: 0,
            });
            effect += 1;
        }
        for header in 0..addressable {
            self.channels.push(Channel {
                kind: Kind::Addressable,
                effect,
                direct: header,
                num_leds: DEFAULT_ADDRESSABLE_LEDS,
                start_led: 0,
            });
            effect = effect.wrapping_add(1);
        }
        if self.channels.is_empty() {
            self.channels.push(fallback());
        }
        log::info!(
            "ASUS Aura: {onboard_leds} onboard LED(s), {rgb_headers} RGB header(s), \
             {addressable} addressable header(s)"
        );
    }

    // -- Packet builders --

    fn send_effect(&mut self, channel: u8, mode: u8, shutdown: bool) -> Result<()> {
        self.write(&[CMD_EFFECT, channel, 0x00, u8::from(shutdown), mode])
    }

    fn send_color(&mut self, start_led: usize, count: usize, color: Rgb, shutdown: bool) -> Result<()> {
        let count = count.min(MAX_ONBOARD_LEDS.saturating_sub(start_led));
        if count == 0 {
            return Ok(());
        }
        let mask = ((1u32 << count) - 1) << start_led;
        let mut packet = vec![CMD_EFFECT_COLOR, (mask >> 8) as u8, mask as u8, u8::from(shutdown)];
        packet.resize(packet.len() + 3 * start_led, 0x00);
        for _ in 0..count {
            packet.extend_from_slice(&color);
        }
        self.write(&packet)
    }

    /// Paint `count` LEDs of one direct device, 20 per packet. The last
    /// packet carries the apply flag.
    fn send_direct(&mut self, device: u8, color: Rgb, count: usize) -> Result<()> {
        let mut offset = 0;
        loop {
            let sent = LEDS_PER_PACKET.min(count - offset);
            let last = offset + sent >= count;
            let mut packet = vec![CMD_DIRECT, if last { 0x80 } else { 0x00 } | device, offset as u8, sent as u8];
            for _ in 0..sent {
                packet.extend_from_slice(&color);
            }
            self.write(&packet)?;
            offset += sent;
            if last {
                return Ok(());
            }
        }
    }

    fn send_commit(&mut self) -> Result<()> {
        self.write(&[CMD_COMMIT, 0x55])
    }

    /// Paint one frame on the given channels in direct mode. The direct-mode
    /// handshake goes out once per effect; later frames only stream LEDs.
    fn render_direct(&mut self, colors: &[(usize, Rgb)]) -> Result<()> {
        if !self.direct_armed {
            for &(idx, _) in colors {
                let effect = self.channels[idx].effect;
                self.send_effect(effect, MODE_DIRECT, false)?;
            }
            self.direct_armed = true;
        }
        for &(idx, color) in colors {
            let (direct, num_leds) = (self.channels[idx].direct, self.channels[idx].num_leds);
            self.send_direct(direct, color, num_leds)?;
        }
        Ok(())
    }

    fn apply_channels(&mut self, colors: &[(usize, Rgb)], mode: u8, shutdown: bool) -> Result<()> {
        for &(idx, color) in colors {
            let ch = &self.channels[idx];
            let (kind, effect, direct, num_leds, start_led) =
                (ch.kind, ch.effect, ch.direct, ch.num_leds, ch.start_led);
            match kind {
                Kind::Fixed => {
                    self.send_effect(effect, mode, shutdown)?;
                    if mode != MODE_OFF {
                        self.send_color(start_led, num_leds, color, shutdown)?;
                    }
                }
                // A solid colour goes out through direct per-LED control
                Kind::Addressable if mode == MODE_OFF || mode == MODE_STATIC => {
                    self.send_effect(effect, MODE_DIRECT, shutdown)?;
                    let fill = if mode == MODE_OFF { [0, 0, 0] } else { color };
                    self.send_direct(direct, fill, num_leds)?;
                }
                // Anything else the hardware animates along the strip
                Kind::Addressable => self.send_effect(effect, mode, shutdown)?,
            }
        }
        Ok(())
    }
}

impl Controller for Aura {
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
        matches!(mode, Mode::ColorCycle | Mode::Rainbow)
    }

    fn set_correction(&mut self, correction: Correction) {
        self.correction = correction;
    }

    fn set_mode(&mut self, mode: Mode, _speed: u8) -> Result<()> {
        // Aura effects carry no speed. A mode change re-arms the direct
        // handshake.
        if mode != self.mode {
            self.direct_armed = false;
        }
        self.mode = mode;
        Ok(())
    }

    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        for zw in zones {
            // The effect is shared, so a dark zone is a black one
            let color = self.correction.apply(zw.color.unwrap_or([0, 0, 0]));
            match usize::from(zw.zone) {
                0 => (0..self.channels.len()).for_each(|idx| self.pending.set(idx, color)),
                zone if zone <= self.channels.len() => self.pending.set(zone - 1, color),
                _ => {}
            }
        }
        if self.pending.0.is_empty() {
            return Ok(());
        }

        let colors = std::mem::take(&mut self.pending.0);
        if software_animated(self.mode) {
            // One frame of a software effect. No commit: that is a flash
            // write and must not run thirty times a second.
            self.render_direct(&colors)?;
        } else {
            self.direct_armed = false;
            self.apply_channels(&colors, mode_value(self.mode), false)?;
            self.send_commit()?;
        }
        for (idx, color) in colors {
            self.applied.set(idx, color);
        }
        Ok(())
    }

    fn supports_save(&self) -> bool {
        true
    }

    /// Store the last applied colours as the boot effect. A software effect
    /// cannot live in the device, so its colour is stored as static.
    fn save(&mut self) -> Result<()> {
        let mode = if software_animated(self.mode) { Mode::Static } else { self.mode };
        let colors = self.applied.0.clone();
        if colors.is_empty() {
            return Ok(());
        }
        self.direct_armed = false;
        self.apply_channels(&colors, mode_value(mode), true)?;
        self.send_commit()
    }
}
