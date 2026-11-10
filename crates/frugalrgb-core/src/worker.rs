//! A thread that owns the devices.
//!
//! Device I/O can take hundreds of milliseconds (memory on SMBus) and
//! software effects need a steady 30 fps, so a UI hands its requests to a
//! [`Worker`] instead of touching hardware itself. Having one owner also
//! means two writers can never interleave packets on a device.

use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::engine::{self, DevicePlan};
use crate::{Controller, Correction, Mode, ZoneWrite};

/// Work a front end wants done on the device thread.
pub type Job = Box<dyn FnOnce(&mut [Box<dyn Controller>]) + Send>;

pub enum Command {
    /// Set `mode` on the planned devices and write their zones.
    Apply { mode: Mode, speed: f64, plans: Vec<DevicePlan> },
    /// Switch every zone of every device off.
    Off,
    Calibrate { device: usize, correction: Correction },
    /// Store the current state in the flash of these devices.
    Save { devices: Vec<usize> },
    /// Run a job with the devices: a diagnostics dump, a device test.
    /// Software effects pause while it runs.
    Run(Job),
    /// Stop the worker, optionally switching the lights off first.
    Shutdown { lights_off: bool },
}

/// What the worker tells the front end, one line per device that failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Event {
    Applied { failures: Vec<String> },
    Saved { failures: Vec<String> },
}

const FRAME: Duration = Duration::from_nanos(1_000_000_000 / 30);

/// A software effect in progress.
struct Animation {
    mode: Mode,
    speed: f64,
    plans: Vec<DevicePlan>,
    /// Effect time in seconds; advances by `speed` frames per frame.
    t: f64,
    next_frame: Instant,
}

pub struct Worker {
    commands: Sender<Command>,
    thread: Option<JoinHandle<()>>,
}

impl Worker {
    /// Hand the devices to a new worker thread. `report` is called on that
    /// thread after every apply and save.
    pub fn spawn(controllers: Vec<Box<dyn Controller>>, report: impl Fn(Event) + Send + 'static) -> Self {
        let (commands, inbox) = mpsc::channel();
        let thread = thread::Builder::new()
            .name("frugalrgb-devices".into())
            .spawn(move || run(controllers, inbox, report))
            .expect("spawning the device thread");
        Worker { commands, thread: Some(thread) }
    }

    pub fn send(&self, command: Command) {
        // The thread only ends on shutdown, after which nothing is sent
        let _ = self.commands.send(command);
    }

    /// Stop the worker and wait until its last write has gone out.
    pub fn shutdown(mut self, lights_off: bool) {
        self.send(Command::Shutdown { lights_off });
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        if let Some(thread) = self.thread.take() {
            let _ = self.commands.send(Command::Shutdown { lights_off: false });
            let _ = thread.join();
        }
    }
}

fn failures(controllers: &[Box<dyn Controller>], failed: Vec<(usize, crate::Error)>) -> Vec<String> {
    failed.into_iter().map(|(device, e)| format!("{}: {e}", controllers[device].name())).collect()
}

fn run(mut controllers: Vec<Box<dyn Controller>>, inbox: Receiver<Command>, report: impl Fn(Event)) {
    let mut animation: Option<Animation> = None;

    loop {
        // Wait for a command, rendering software frames in the meantime
        let command = match &mut animation {
            None => match inbox.recv() {
                Ok(command) => command,
                Err(_) => return,
            },
            Some(anim) => {
                let wait = anim.next_frame.saturating_duration_since(Instant::now());
                match inbox.recv_timeout(wait) {
                    Ok(command) => command,
                    Err(RecvTimeoutError::Timeout) => {
                        render(&mut controllers, anim);
                        continue;
                    }
                    Err(RecvTimeoutError::Disconnected) => return,
                }
            }
        };

        // A slider being dragged queues many applies. Only the newest one
        // matters; everything else still runs in order.
        let mut batch = vec![command];
        batch.extend(inbox.try_iter());
        let last_apply = batch.iter().rposition(|c| matches!(c, Command::Apply { .. }));

        for (index, command) in batch.into_iter().enumerate() {
            match command {
                Command::Apply { .. } if Some(index) != last_apply => {}
                Command::Apply { mode, speed, plans } => {
                    let failed = engine::apply(&mut controllers, mode, speed, &plans);
                    let software = engine::software_plans(&controllers, mode, &plans);
                    animation = (!software.is_empty()).then(|| Animation {
                        mode,
                        speed,
                        plans: software,
                        t: 0.0,
                        next_frame: Instant::now(),
                    });
                    report(Event::Applied { failures: failures(&controllers, failed) });
                }
                Command::Off => {
                    animation = None;
                    let failed = engine::turn_off(&mut controllers);
                    report(Event::Applied { failures: failures(&controllers, failed) });
                }
                Command::Calibrate { device, correction } => {
                    if let Some(ctrl) = controllers.get_mut(device) {
                        ctrl.set_correction(correction);
                    }
                }
                Command::Save { devices } => {
                    let mut failed = Vec::new();
                    for device in devices {
                        let Some(ctrl) = controllers.get_mut(device) else {
                            continue;
                        };
                        if let Err(e) = ctrl.save() {
                            log::error!("Failed to save to hardware on {}: {e}", ctrl.name());
                            failed.push(format!("{}: {e}", ctrl.name()));
                        }
                    }
                    report(Event::Saved { failures: failed });
                }
                Command::Run(job) => job(&mut controllers),
                Command::Shutdown { lights_off } => {
                    if lights_off {
                        engine::turn_off(&mut controllers);
                    }
                    return;
                }
            }
        }
    }
}

/// Send one frame of a software effect. Dark zones stay dark.
fn render(controllers: &mut [Box<dyn Controller>], anim: &mut Animation) {
    for plan in &anim.plans {
        let Some(ctrl) = controllers.get_mut(plan.device) else {
            continue;
        };
        let frame: Vec<ZoneWrite> = plan
            .zones
            .iter()
            .map(|zw| ZoneWrite {
                zone: zw.zone,
                color: zw.color.map(|base| engine::frame_color(anim.mode, anim.t, base)),
            })
            .collect();
        // A dropped frame is replaced by the next one 33 ms later
        let _ = ctrl.write_zones(&frame);
    }
    anim.t += FRAME.as_secs_f64() * anim.speed;
    // Keep the cadence, but never try to catch up on frames a slow bus cost
    anim.next_frame = (anim.next_frame + FRAME).max(Instant::now());
}
