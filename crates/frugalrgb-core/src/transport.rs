//! The two ways frugalRGB reaches hardware. Controllers only ever see these
//! traits, which keeps every protocol testable without the device.

use crate::Result;

/// A HID device. Buffers follow the hidapi convention: byte 0 of everything
/// sent is the report ID (0 for devices without numbered reports).
pub trait HidTransport: Send {
    /// Send an output report.
    fn write(&mut self, data: &[u8]) -> Result<()>;

    /// Read one input report of at most `len` bytes. An empty vector means
    /// nothing arrived within `timeout_ms`.
    fn read(&mut self, len: usize, timeout_ms: i32) -> Result<Vec<u8>>;

    fn send_feature_report(&mut self, data: &[u8]) -> Result<()>;

    /// Read a feature report of at most `len` bytes, report ID included.
    fn get_feature_report(&mut self, report_id: u8, len: usize) -> Result<Vec<u8>>;
}

/// An SMBus / I2C adapter, addressed per transfer.
pub trait SmBus: Send {
    fn read_byte_data(&mut self, addr: u8, cmd: u8) -> Result<u8>;
    fn write_byte_data(&mut self, addr: u8, cmd: u8, value: u8) -> Result<()>;
    fn write_word_data(&mut self, addr: u8, cmd: u8, value: u16) -> Result<()>;
    fn write_block_data(&mut self, addr: u8, cmd: u8, data: &[u8]) -> Result<()>;
}
