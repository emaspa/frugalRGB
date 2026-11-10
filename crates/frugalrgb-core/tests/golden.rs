//! Protocol parity with the Python implementation.
//!
//! `tools/gen_golden.py` drove each Python controller through a set of
//! scenarios against a fake device and recorded every byte exchanged. Here
//! the Rust controllers run the same scenarios against a transport that
//! replays those recordings: any write that differs, comes in a different
//! order, or is missing fails the test.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use frugalrgb_core::controllers::ene::{self, EneV2, SharedBus};
use frugalrgb_core::controllers::{asrock::Asrock, aura::Aura, gigabyte::Gigabyte, katana::Katana, msi::Msi};
use frugalrgb_core::transport::{HidTransport, SmBus};
use frugalrgb_core::{Controller, Correction, Mode, Result, Rgb, ZoneWrite};
use serde::Deserialize;

#[derive(Deserialize)]
struct Case {
    name: String,
    device_name: String,
    /// `[id, name, is_all]`
    zones: Vec<(u8, String, bool)>,
    ops: Vec<Op>,
    /// `[kind, hex bytes]`, kind being w / r / sf / gf
    transcript: Vec<(String, String)>,
    #[serde(default)]
    pid: u16,
    #[serde(default)]
    product: String,
    /// Which of the detected devices the scenario drives.
    #[serde(default)]
    pick: usize,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Op {
    Correction { value: [f64; 3] },
    Apply { mode: String, speed: u8, zones: Vec<(u8, Option<Rgb>)> },
    Frame { zones: Vec<(u8, Option<Rgb>)> },
    Save,
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

struct Recording {
    case: String,
    events: VecDeque<(String, Vec<u8>)>,
    position: usize,
}

impl Recording {
    /// The device is being sent `data`: it must be the next recorded event.
    fn expect(&mut self, kind: &str, data: &[u8]) {
        let at = self.position;
        match self.events.pop_front() {
            Some((k, expected)) if k == kind && expected == data => self.position += 1,
            Some((k, expected)) => panic!(
                "{}: event {at} differs\n  python: {k} {}\n  rust:   {kind} {}",
                self.case,
                hex(&expected),
                hex(data)
            ),
            None => panic!(
                "{}: rust sent an extra {kind} {} after the recording ended",
                self.case,
                hex(data)
            ),
        }
    }

    /// The device is being read: hand back the recorded reply, if the next
    /// event is one.
    fn reply(&mut self, kind: &str) -> Option<Vec<u8>> {
        if self.events.front().is_some_and(|(k, _)| k == kind) {
            self.position += 1;
            return self.events.pop_front().map(|(_, data)| data);
        }
        None
    }
}

#[derive(Clone)]
struct Replay(Arc<Mutex<Recording>>);

impl Replay {
    fn new(case: &Case, suite: &str) -> Self {
        Replay(Arc::new(Mutex::new(Recording {
            case: format!("{suite}/{}", case.name),
            events: case.transcript.iter().map(|(k, h)| (k.clone(), unhex(h))).collect(),
            position: 0,
        })))
    }

    fn assert_finished(&self) {
        let rec = self.0.lock().unwrap();
        if let Some((kind, data)) = rec.events.front() {
            panic!(
                "{}: rust stopped at event {}, python went on with {kind} {} ({} more)",
                rec.case,
                rec.position,
                hex(data),
                rec.events.len()
            );
        }
    }
}

impl HidTransport for Replay {
    fn write(&mut self, data: &[u8]) -> Result<()> {
        self.0.lock().unwrap().expect("w", data);
        Ok(())
    }

    fn read(&mut self, len: usize, _timeout_ms: i32) -> Result<Vec<u8>> {
        // Nothing recorded here means nothing arrived: a timeout
        let mut data = self.0.lock().unwrap().reply("r").unwrap_or_default();
        data.truncate(len);
        Ok(data)
    }

    fn send_feature_report(&mut self, data: &[u8]) -> Result<()> {
        self.0.lock().unwrap().expect("sf", data);
        Ok(())
    }

    fn get_feature_report(&mut self, _report_id: u8, len: usize) -> Result<Vec<u8>> {
        let mut rec = self.0.lock().unwrap();
        let Some(mut data) = rec.reply("gf") else {
            panic!("{}: rust read a feature report python did not (event {})", rec.case, rec.position);
        };
        data.truncate(len);
        Ok(data)
    }
}

/// SMBus events are `[addr, cmd, value...]`; a `_nak` kind is a transfer the
/// device did not acknowledge.
impl Recording {
    fn bus_write(&mut self, kind: &str, bytes: &[u8]) -> Result<()> {
        let nak = format!("{kind}_nak");
        if self.events.front().is_some_and(|(k, data)| *k == nak && data == bytes) {
            self.events.pop_front();
            self.position += 1;
            return Err(frugalrgb_core::Error::Io("no such device".into()));
        }
        self.expect(kind, bytes);
        Ok(())
    }
}

impl SmBus for Replay {
    fn read_byte_data(&mut self, addr: u8, cmd: u8) -> Result<u8> {
        let mut rec = self.0.lock().unwrap();
        let at = rec.position;
        match rec.events.pop_front() {
            Some((k, data)) if k == "rb" && data[..2] == [addr, cmd] => {
                rec.position += 1;
                Ok(data[2])
            }
            Some((k, data)) if k == "rb_nak" && data == [addr, cmd] => {
                rec.position += 1;
                Err(frugalrgb_core::Error::Io("no such device".into()))
            }
            Some((k, data)) => panic!(
                "{}: event {at} differs\n  python: {k} {}\n  rust:   rb {}",
                rec.case,
                hex(&data),
                hex(&[addr, cmd])
            ),
            None => panic!("{}: rust read {addr:02x}/{cmd:02x} after the recording ended", rec.case),
        }
    }

    fn write_byte_data(&mut self, addr: u8, cmd: u8, value: u8) -> Result<()> {
        self.0.lock().unwrap().bus_write("wb", &[addr, cmd, value])
    }

    fn write_word_data(&mut self, addr: u8, cmd: u8, value: u16) -> Result<()> {
        let [lo, hi] = value.to_le_bytes();
        self.0.lock().unwrap().bus_write("ww", &[addr, cmd, lo, hi])
    }

    fn write_block_data(&mut self, _addr: u8, _cmd: u8, _data: &[u8]) -> Result<()> {
        panic!("no controller uses block writes");
    }
}

fn zone_writes(zones: &[(u8, Option<Rgb>)]) -> Vec<ZoneWrite> {
    zones.iter().map(|&(zone, color)| ZoneWrite { zone, color }).collect()
}

/// Run every case of `suite` on a controller made by `build`.
fn check(suite: &str, build: impl Fn(Replay, &Case) -> Box<dyn Controller>) {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/golden").join(format!("{suite}.json"));
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let cases: Vec<Case> = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    assert!(!cases.is_empty(), "{suite}: no cases recorded");

    for case in &cases {
        let replay = Replay::new(case, suite);
        let mut ctrl = build(replay.clone(), case);
        let context = format!("{suite}/{}", case.name);

        assert_eq!(ctrl.name(), case.device_name, "{context}: device name");
        let zones: Vec<(u8, String, bool)> =
            ctrl.zones().iter().map(|z| (z.id, z.name.clone(), z.is_all)).collect();
        assert_eq!(zones, case.zones, "{context}: zones");

        for op in &case.ops {
            let done = match op {
                Op::Correction { value } => {
                    ctrl.set_correction(Correction(*value));
                    Ok(())
                }
                Op::Apply { mode, speed, zones } => {
                    let mode = Mode::from_effect(mode).expect("known mode");
                    ctrl.apply(mode, *speed, &zone_writes(zones))
                }
                Op::Frame { zones } => ctrl.write_zones(&zone_writes(zones)),
                Op::Save => ctrl.save(),
            };
            if let Err(e) = done {
                panic!("{context}: {e}");
            }
        }
        replay.assert_finished();
    }
}

fn build_gigabyte(replay: Replay, case: &Case) -> Box<dyn Controller> {
    // Detection probes the interface before taking the device
    assert!(Gigabyte::responds(&mut replay.clone()));
    Box::new(Gigabyte::new(Box::new(replay), case.pid).unwrap())
}

fn build_katana(replay: Replay, _case: &Case) -> Box<dyn Controller> {
    Box::new(Katana::probe(Box::new(replay)).unwrap().expect("katana answers the probe"))
}

fn shared(replay: Replay) -> SharedBus {
    Arc::new(Mutex::new(replay))
}

fn build_ene_ddr5(replay: Replay, case: &Case) -> Box<dyn Controller> {
    let mut found = ene::detect_ddr5(&shared(replay));
    assert_eq!(found.iter().map(|(addr, _)| *addr).collect::<Vec<_>>(), [0x70, 0x71, 0x72]);
    Box::new(found.swap_remove(case.pick).1)
}

fn build_ene_dram(replay: Replay, case: &Case) -> Box<dyn Controller> {
    let mut found = ene::detect_dram(&shared(replay), &[]);
    assert_eq!(found.len(), 1, "only the stick at 0x72 is a plain ENE DRAM");
    Box::new(found.swap_remove(case.pick))
}

fn build_asus_gpu(replay: Replay, case: &Case) -> Box<dyn Controller> {
    // Detection reads the chip name before handing the bus over
    assert_eq!(ene::read_name(&mut replay.clone(), ene::GPU_ADDR).as_deref(), Some("AUMA0-E6K5-0107"));
    Box::new(EneV2::gpu(shared(replay), &case.device_name).unwrap())
}

fn build_aura(replay: Replay, case: &Case) -> Box<dyn Controller> {
    Box::new(Aura::new(Box::new(replay), case.pid, &case.product).unwrap())
}

fn build_msi(replay: Replay, case: &Case) -> Box<dyn Controller> {
    assert!(Msi::responds(&mut replay.clone()));
    Box::new(Msi::new(Box::new(replay), case.pid).unwrap())
}

fn build_asrock(replay: Replay, _case: &Case) -> Box<dyn Controller> {
    Box::new(Asrock::new(Box::new(replay)).unwrap())
}

macro_rules! suites {
    ($($name:ident => $build:ident),* $(,)?) => {
        $(#[test]
        fn $name() {
            check(stringify!($name), $build);
        })*
    };
}

suites! {
    ene_ddr5 => build_ene_ddr5,
    ene_dram => build_ene_dram,
    asus_gpu_4_leds => build_asus_gpu,
    asus_gpu_1_led => build_asus_gpu,
    aura => build_aura,
    aura_no_config_table => build_aura,
    aura_stripped_report_id => build_aura,
    aura_addressable_only => build_aura,
    aura_many_onboard_leds => build_aura,
    msi_known_board => build_msi,
    msi_unknown_board => build_msi,
    msi_short_report => build_msi,
    asrock => build_asrock,
    asrock_no_replies => build_asrock,
}

#[test]
fn gigabyte_it5711() {
    check("gigabyte_it5711", build_gigabyte);
}

#[test]
fn gigabyte_it5702() {
    check("gigabyte_it5702", build_gigabyte);
}

#[test]
fn gigabyte_it8297() {
    check("gigabyte_it8297", build_gigabyte);
}

#[test]
fn katana_acks_everything() {
    check("katana", build_katana);
}

#[test]
fn katana_rejects_pulsate_colour() {
    check("katana_rejects_pulsate_colour", build_katana);
}

#[test]
fn katana_with_async_events() {
    check("katana_with_async_events", build_katana);
}
