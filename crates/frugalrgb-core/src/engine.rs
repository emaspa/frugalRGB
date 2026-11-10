//! Turns what the user asked for into device writes.
//!
//! [`resolve`] maps the UI's zone cards onto per-device zone states,
//! [`apply`] writes them, and [`run_software`] animates the devices that
//! cannot run an effect in hardware.

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::{Controller, DeviceInfo, Error, Mode, Rgb, ZoneWrite};

/// One zone card: a zone of the device at index `device`, its checkbox and
/// its colour.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Card {
    pub device: usize,
    pub zone: u8,
    pub enabled: bool,
    pub color: Rgb,
}

/// The zone states to write to one device, in write order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DevicePlan {
    pub device: usize,
    pub zones: Vec<ZoneWrite>,
}

/// Turn zone cards into the state to write to each device.
///
/// A checked zone is lit with its colour and an unchecked zone is driven
/// dark, so what the cards show is what the hardware gets. A device with no
/// checked zone at all is left untouched. An "All Zones" card is written
/// first and covers LEDs that have no card of their own; every other zone
/// then gets its own state on top of it.
pub fn resolve(cards: &[Card], devices: &[DeviceInfo]) -> Vec<DevicePlan> {
    let mut plans: Vec<DevicePlan> = Vec::new();
    let mut any_enabled: Vec<bool> = Vec::new();

    for card in cards {
        let slot = match plans.iter().position(|p| p.device == card.device) {
            Some(slot) => slot,
            None => {
                plans.push(DevicePlan { device: card.device, zones: Vec::new() });
                any_enabled.push(false);
                plans.len() - 1
            }
        };
        any_enabled[slot] |= card.enabled;
        plans[slot].zones.push(ZoneWrite {
            zone: card.zone,
            color: card.enabled.then_some(card.color),
        });
    }

    let mut keep = any_enabled.into_iter();
    plans.retain(|_| keep.next().unwrap_or(false));

    for plan in &mut plans {
        let Some(device) = devices.get(plan.device) else {
            continue;
        };
        let is_all = |zone: u8| device.zones.iter().any(|z| z.id == zone && z.is_all);
        // Stable: the all-zones write goes first, the rest keep their order
        plan.zones.sort_by_key(|zw| !is_all(zw.zone));
    }
    plans
}

/// The speed slider (0.2 to 3.0) as the 0-5 step devices take.
fn speed_step(speed: f64) -> u8 {
    speed.clamp(0.0, 5.0) as u8
}

/// Set `mode` on every planned device and write its zones. Devices are
/// independent: one failing does not stop the others. Returns the failures.
pub fn apply(
    controllers: &mut [Box<dyn Controller>],
    mode: Mode,
    speed: f64,
    plans: &[DevicePlan],
) -> Vec<(usize, Error)> {
    let mut failures = Vec::new();
    for plan in plans {
        let Some(ctrl) = controllers.get_mut(plan.device) else {
            continue;
        };
        if let Err(e) = ctrl.apply(mode, speed_step(speed), &plan.zones) {
            log::warn!("Failed to apply {} on {}: {e}", mode.effect_name(), ctrl.name());
            failures.push((plan.device, e));
        }
    }
    failures
}

/// Switch every zone of every device off.
pub fn turn_off(controllers: &mut [Box<dyn Controller>]) -> Vec<(usize, Error)> {
    let plans: Vec<DevicePlan> = controllers
        .iter()
        .enumerate()
        .map(|(device, ctrl)| DevicePlan {
            device,
            zones: ctrl.zones().iter().map(|z| ZoneWrite::lit(z.id, [0, 0, 0])).collect(),
        })
        .collect();
    apply(controllers, Mode::Off, 1.0, &plans)
}

/// The plans whose device cannot animate `mode` itself and therefore needs
/// [`run_software`]. Streaming frames to a device that runs the effect in
/// hardware would fight its own animation.
pub fn software_plans(
    controllers: &[Box<dyn Controller>],
    mode: Mode,
    plans: &[DevicePlan],
) -> Vec<DevicePlan> {
    if !mode.is_animated() {
        return Vec::new();
    }
    plans
        .iter()
        .filter(|p| controllers.get(p.device).is_some_and(|c| !c.hardware_animates(mode)))
        .cloned()
        .collect()
}

const FRAME_TIME: f64 = 1.0 / 30.0;

/// The colour a lit zone shows `t` effect-seconds into `mode`.
pub fn frame_color(mode: Mode, t: f64, base: Rgb) -> Rgb {
    match mode {
        Mode::Breathing => {
            let brightness = ((t * 2.0).sin() + 1.0) / 2.0;
            base.map(|v| (f64::from(v) * brightness) as u8)
        }
        Mode::ColorCycle => hue_to_rgb((t * 0.1).rem_euclid(1.0)),
        Mode::Rainbow => hue_to_rgb((t * 0.05).rem_euclid(1.0)),
        Mode::Strobe => {
            if (t * 4.0) as u64 % 2 == 0 {
                base
            } else {
                [0, 0, 0]
            }
        }
        Mode::Off | Mode::Static => base,
    }
}

/// Fully saturated, full-value HSV to RGB.
fn hue_to_rgb(hue: f64) -> Rgb {
    let sector = (hue * 6.0) as u32;
    let rising = hue * 6.0 - f64::from(sector);
    let falling = 1.0 - rising;
    let (r, g, b) = match sector % 6 {
        0 => (1.0, rising, 0.0),
        1 => (falling, 1.0, 0.0),
        2 => (0.0, 1.0, rising),
        3 => (0.0, falling, 1.0),
        4 => (rising, 0.0, 1.0),
        _ => (1.0, 0.0, falling),
    };
    [(r * 255.0) as u8, (g * 255.0) as u8, (b * 255.0) as u8]
}

/// Stream frames of `mode` to the planned devices at 30 fps until `stop` is
/// set. Call [`apply`] first so each device is in the right mode. Dark zones
/// stay dark.
pub fn run_software(
    controllers: &mut [Box<dyn Controller>],
    mode: Mode,
    speed: f64,
    plans: &[DevicePlan],
    stop: &AtomicBool,
) {
    let mut t = 0.0_f64;
    while !stop.load(Ordering::Relaxed) {
        let started = Instant::now();

        for plan in plans {
            let Some(ctrl) = controllers.get_mut(plan.device) else {
                continue;
            };
            let frame: Vec<ZoneWrite> = plan
                .zones
                .iter()
                .map(|zw| ZoneWrite {
                    zone: zw.zone,
                    color: zw.color.map(|base| frame_color(mode, t, base)),
                })
                .collect();
            // A dropped frame is replaced by the next one 33 ms later
            let _ = ctrl.write_zones(&frame);
        }

        t += FRAME_TIME * speed;
        let remaining = FRAME_TIME - started.elapsed().as_secs_f64();
        if remaining > 0.0 {
            sleep(Duration::from_secs_f64(remaining));
        }
    }
}
