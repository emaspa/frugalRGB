//! [`HidTransport`] on top of the hidapi crate, plus device enumeration.

use std::ffi::CString;

use hidapi::{HidApi, HidDevice};

use crate::transport::HidTransport;
use crate::{Error, Result};

fn io(context: &str, e: hidapi::HidError) -> Error {
    Error::Io(format!("{context}: {e}"))
}

/// An open HID device.
pub struct HidDev(HidDevice);

impl HidTransport for HidDev {
    fn write(&mut self, data: &[u8]) -> Result<()> {
        self.0.write(data).map(|_| ()).map_err(|e| io("HID write", e))
    }

    fn read(&mut self, len: usize, timeout_ms: i32) -> Result<Vec<u8>> {
        let mut buf = vec![0u8; len];
        let n = self.0.read_timeout(&mut buf, timeout_ms).map_err(|e| io("HID read", e))?;
        buf.truncate(n);
        Ok(buf)
    }

    fn send_feature_report(&mut self, data: &[u8]) -> Result<()> {
        self.0.send_feature_report(data).map_err(|e| io("HID feature write", e))
    }

    fn get_feature_report(&mut self, report_id: u8, len: usize) -> Result<Vec<u8>> {
        let mut buf = vec![0u8; len];
        buf[0] = report_id;
        let n = self.0.get_feature_report(&mut buf).map_err(|e| io("HID feature read", e))?;
        buf.truncate(n);
        Ok(buf)
    }
}

/// One HID interface as the OS lists it.
#[derive(Clone, Debug)]
pub struct HidInfo {
    pub vendor_id: u16,
    pub product_id: u16,
    /// Every (usage page, usage) the interface declares. Empty when the
    /// backend cannot tell.
    pub usages: Vec<(u16, u16)>,
    /// USB interface number, or -1 when the backend cannot tell.
    pub interface: i32,
    pub product: String,
    path: CString,
}

impl HidInfo {
    pub fn path(&self) -> String {
        self.path.to_string_lossy().into_owned()
    }

    pub fn has_usage(&self, page: u16, usage: u16) -> bool {
        self.usages.contains(&(page, usage))
    }
}

/// The system's HID devices.
pub struct HidBus(HidApi);

impl HidBus {
    pub fn new() -> Result<Self> {
        HidApi::new().map(HidBus).map_err(|e| io("HID enumeration", e))
    }

    /// Interfaces of `vendor_id`, one entry per device path.
    pub fn devices(&self, vendor_id: u16) -> Vec<HidInfo> {
        let mut found: Vec<HidInfo> = Vec::new();
        for d in self.0.device_list().filter(|d| d.vendor_id() == vendor_id) {
            // Backends list a path once per top-level usage
            let usage = (d.usage_page() != 0).then(|| (d.usage_page(), d.usage()));
            if let Some(seen) = found.iter_mut().find(|f| f.path.as_c_str() == d.path()) {
                seen.usages.extend(usage);
                continue;
            }
            found.push(HidInfo {
                vendor_id: d.vendor_id(),
                product_id: d.product_id(),
                usages: usage.into_iter().collect(),
                interface: d.interface_number(),
                product: d.product_string().unwrap_or_default().trim().to_string(),
                path: d.path().to_owned(),
            });
        }
        found
    }

    /// One line per HID interface on the system, for diagnostics.
    pub fn describe_all(&self) -> Vec<String> {
        let mut lines: Vec<String> = self
            .0
            .device_list()
            .map(|d| {
                format!(
                    "{:04X}:{:04X} usage {:04X}/{:04X} interface {:>2}  {} {}",
                    d.vendor_id(),
                    d.product_id(),
                    d.usage_page(),
                    d.usage(),
                    d.interface_number(),
                    d.manufacturer_string().unwrap_or_default().trim(),
                    d.product_string().unwrap_or_default().trim()
                )
            })
            .collect();
        lines.sort();
        lines.dedup();
        lines
    }

    pub fn open(&self, info: &HidInfo) -> Result<HidDev> {
        self.0.open_path(&info.path).map(HidDev).map_err(|e| io("HID open", e))
    }
}
