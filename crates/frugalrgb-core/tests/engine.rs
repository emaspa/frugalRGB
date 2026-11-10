//! The zone resolver, the apply path and the software animation maths.

use std::sync::{Arc, Mutex};

use frugalrgb_core::engine::{self, Card, DevicePlan};
use frugalrgb_core::{Controller, Correction, DeviceInfo, Mode, Result, Rgb, Zone, ZoneWrite};

const ORANGE: Rgb = [255, 128, 0];
const BLUE: Rgb = [0, 0, 255];

#[derive(Debug, PartialEq, Clone)]
enum Call {
    Mode(Mode, u8),
    Zones(Vec<ZoneWrite>),
}

struct Fake {
    zones: Vec<Zone>,
    hardware: bool,
    calls: Arc<Mutex<Vec<Call>>>,
}

impl Controller for Fake {
    fn name(&self) -> &str {
        "Fake"
    }
    fn zones(&self) -> &[Zone] {
        &self.zones
    }
    fn supported_modes(&self) -> &'static [Mode] {
        &Mode::ALL
    }
    fn hardware_animates(&self, _mode: Mode) -> bool {
        self.hardware
    }
    fn set_correction(&mut self, _correction: Correction) {}
    fn set_mode(&mut self, mode: Mode, speed: u8) -> Result<()> {
        self.calls.lock().unwrap().push(Call::Mode(mode, speed));
        Ok(())
    }
    fn write_zones(&mut self, zones: &[ZoneWrite]) -> Result<()> {
        self.calls.lock().unwrap().push(Call::Zones(zones.to_vec()));
        Ok(())
    }
}

fn multi_zone(hardware: bool) -> (Box<dyn Controller>, Arc<Mutex<Vec<Call>>>) {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let fake = Fake {
        zones: vec![Zone::all(0, "All Zones"), Zone::new(1, "Logo"), Zone::new(2, "Accent")],
        hardware,
        calls: Arc::clone(&calls),
    };
    (Box::new(fake), calls)
}

fn card(device: usize, zone: u8, enabled: bool, color: Rgb) -> Card {
    Card { device, zone, enabled, color }
}

#[test]
fn unchecked_zone_is_off_even_when_all_zones_is_checked() {
    let controllers = vec![multi_zone(true).0];
    let plans = engine::resolve(
        &[card(0, 0, true, ORANGE), card(0, 1, true, ORANGE), card(0, 2, false, [0, 0, 0])],
        &DeviceInfo::all(&controllers),
    );
    assert_eq!(
        plans,
        [DevicePlan {
            device: 0,
            zones: vec![ZoneWrite::lit(0, ORANGE), ZoneWrite::lit(1, ORANGE), ZoneWrite::off(2)],
        }]
    );
}

#[test]
fn all_zones_is_written_before_the_zones_it_covers() {
    let controllers = vec![multi_zone(true).0];
    let plans = engine::resolve(
        &[card(0, 1, true, BLUE), card(0, 2, false, ORANGE), card(0, 0, true, ORANGE)],
        &DeviceInfo::all(&controllers),
    );
    let order: Vec<u8> = plans[0].zones.iter().map(|zw| zw.zone).collect();
    assert_eq!(order, [0, 1, 2]);
}

#[test]
fn unchecked_all_zones_turns_the_rest_of_the_device_off() {
    let controllers = vec![multi_zone(true).0];
    let plans = engine::resolve(
        &[card(0, 0, false, ORANGE), card(0, 1, true, BLUE), card(0, 2, false, ORANGE)],
        &DeviceInfo::all(&controllers),
    );
    assert_eq!(plans[0].zones, [ZoneWrite::off(0), ZoneWrite::lit(1, BLUE), ZoneWrite::off(2)]);
}

#[test]
fn device_with_nothing_checked_is_left_alone() {
    let controllers = vec![multi_zone(true).0, multi_zone(true).0];
    let plans = engine::resolve(
        &[card(0, 0, false, ORANGE), card(0, 1, false, ORANGE), card(1, 0, true, ORANGE)],
        &DeviceInfo::all(&controllers),
    );
    assert_eq!(plans, [DevicePlan { device: 1, zones: vec![ZoneWrite::lit(0, ORANGE)] }]);
}

#[test]
fn zone_zero_is_a_real_zone_when_not_flagged_all() {
    // ASRock numbers its physical zones from 0 and has no all-zones card
    let fake = Fake {
        zones: vec![Zone::new(0, "RGB Header 1"), Zone::new(1, "PCH")],
        hardware: true,
        calls: Arc::default(),
    };
    let controllers: Vec<Box<dyn Controller>> = vec![Box::new(fake)];
    let plans = engine::resolve(&[card(0, 1, true, BLUE), card(0, 0, false, ORANGE)], &DeviceInfo::all(&controllers));
    assert_eq!(plans[0].zones, [ZoneWrite::lit(1, BLUE), ZoneWrite::off(0)]);
}

#[test]
fn apply_sets_the_mode_once_then_writes_the_zones() {
    let (ctrl, calls) = multi_zone(true);
    let mut controllers = vec![ctrl];
    let zones = vec![ZoneWrite::lit(0, ORANGE), ZoneWrite::off(2)];
    let plans = [DevicePlan { device: 0, zones: zones.clone() }];

    let failures = engine::apply(&mut controllers, Mode::ColorCycle, 2.7, &plans);

    assert!(failures.is_empty());
    assert_eq!(*calls.lock().unwrap(), [Call::Mode(Mode::ColorCycle, 2), Call::Zones(zones)]);
}

#[test]
fn turn_off_blacks_out_every_zone_in_off_mode() {
    let (ctrl, calls) = multi_zone(true);
    let mut controllers = vec![ctrl];
    engine::turn_off(&mut controllers);
    let black = |zone| ZoneWrite::lit(zone, [0, 0, 0]);
    assert_eq!(
        *calls.lock().unwrap(),
        [Call::Mode(Mode::Off, 1), Call::Zones(vec![black(0), black(1), black(2)])]
    );
}

#[test]
fn only_devices_without_the_effect_in_hardware_need_the_software_loop() {
    let controllers = vec![multi_zone(true).0, multi_zone(false).0];
    let plans = [
        DevicePlan { device: 0, zones: vec![ZoneWrite::lit(0, ORANGE)] },
        DevicePlan { device: 1, zones: vec![ZoneWrite::lit(0, ORANGE)] },
    ];
    let software = engine::software_plans(&controllers, Mode::Breathing, &plans);
    assert_eq!(software.iter().map(|p| p.device).collect::<Vec<_>>(), [1]);
    assert!(engine::software_plans(&controllers, Mode::Static, &plans).is_empty());
}

#[test]
fn software_frames_match_the_python_engine() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/software_frames.json");
    let rows: Vec<(String, f64, Rgb, Rgb)> =
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    assert!(rows.len() > 1000);
    for (effect, t, base, expected) in rows {
        let mode = Mode::from_effect(&effect).unwrap();
        assert_eq!(engine::frame_color(mode, t, base), expected, "{effect} at t={t} from {base:?}");
    }
}

mod worker {
    use std::sync::mpsc;
    use std::time::Duration;

    use super::*;
    use frugalrgb_core::worker::{Command, Event, Worker};

    fn plan(zones: Vec<ZoneWrite>) -> Vec<DevicePlan> {
        vec![DevicePlan { device: 0, zones }]
    }

    #[test]
    fn applies_reports_and_switches_off_on_shutdown() {
        let (ctrl, calls) = multi_zone(true);
        let (events, inbox) = mpsc::channel();
        let worker = Worker::spawn(vec![ctrl], move |e| events.send(e).unwrap());

        let zones = vec![ZoneWrite::lit(0, ORANGE), ZoneWrite::off(2)];
        worker.send(Command::Apply { mode: Mode::Static, speed: 1.0, plans: plan(zones.clone()) });
        assert_eq!(inbox.recv_timeout(Duration::from_secs(5)).unwrap(), Event::Applied { failures: vec![] });

        worker.shutdown(true);
        let black = |zone| ZoneWrite::lit(zone, [0, 0, 0]);
        assert_eq!(
            *calls.lock().unwrap(),
            [
                Call::Mode(Mode::Static, 1),
                Call::Zones(zones),
                Call::Mode(Mode::Off, 1),
                Call::Zones(vec![black(0), black(1), black(2)]),
            ]
        );
    }

    #[test]
    fn streams_frames_to_devices_without_the_effect_in_hardware() {
        let (ctrl, calls) = multi_zone(false);
        let (events, inbox) = mpsc::channel();
        let worker = Worker::spawn(vec![ctrl], move |e| events.send(e).unwrap());

        let zones = vec![ZoneWrite::lit(1, ORANGE), ZoneWrite::off(2)];
        worker.send(Command::Apply { mode: Mode::Strobe, speed: 1.0, plans: plan(zones) });
        inbox.recv_timeout(Duration::from_secs(5)).unwrap();
        std::thread::sleep(Duration::from_millis(250));
        worker.shutdown(false);

        let calls = calls.lock().unwrap();
        let frames: Vec<&Vec<ZoneWrite>> =
            calls.iter().skip(2).filter_map(|c| if let Call::Zones(z) = c { Some(z) } else { None }).collect();
        assert!(frames.len() >= 4, "expected a stream of frames, got {}", frames.len());
        // The dark zone stays dark in every frame; the lit one keeps its id
        assert!(frames.iter().all(|f| f[1] == ZoneWrite::off(2) && f[0].zone == 1 && f[0].color.is_some()));
        // No mode change between frames
        assert_eq!(calls.iter().filter(|c| matches!(c, Call::Mode(..))).count(), 1);
    }

    #[test]
    fn hardware_effects_are_written_once() {
        let (ctrl, calls) = multi_zone(true);
        let (events, inbox) = mpsc::channel();
        let worker = Worker::spawn(vec![ctrl], move |e| events.send(e).unwrap());
        worker.send(Command::Apply {
            mode: Mode::Breathing,
            speed: 1.0,
            plans: plan(vec![ZoneWrite::lit(0, ORANGE)]),
        });
        inbox.recv_timeout(Duration::from_secs(5)).unwrap();
        std::thread::sleep(Duration::from_millis(120));
        worker.shutdown(false);
        assert_eq!(calls.lock().unwrap().len(), 2);
    }
}
