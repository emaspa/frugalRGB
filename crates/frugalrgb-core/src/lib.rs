//! Device protocols and effect engine for frugalRGB.
//!
//! Controllers talk to hardware through the small traits in [`transport`],
//! so every protocol can be exercised in tests against a recorded transcript
//! without the device being present.

pub mod controllers;
pub mod detect;
pub mod diagnostics;
pub mod engine;
pub mod hid;
#[cfg(windows)]
pub mod nvapi;
#[cfg(target_os = "linux")]
pub mod smbus_linux;
#[cfg(windows)]
pub mod smbus_windows;
#[cfg(windows)]
pub mod win;
pub mod store;
pub mod transport;
mod types;
pub mod worker;

pub use controllers::{Controller, DeviceInfo};
pub use types::{Correction, Error, Mode, Result, Rgb, Zone, ZoneWrite};
