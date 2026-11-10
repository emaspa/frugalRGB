//! [`SmBus`] on Linux through the kernel's i2c-dev interface (`/dev/i2c-N`).

use std::fs::{self, File, OpenOptions};
use std::os::fd::AsRawFd;
use std::path::PathBuf;

use crate::transport::SmBus;
use crate::{Error, Result};

const I2C_SLAVE: libc::c_ulong = 0x0703;
const I2C_SMBUS: libc::c_ulong = 0x0720;

const SMBUS_WRITE: u8 = 0;
const SMBUS_READ: u8 = 1;
const SIZE_BYTE_DATA: u32 = 2;
const SIZE_WORD_DATA: u32 = 3;
const SIZE_BLOCK_DATA: u32 = 5;
const BLOCK_MAX: usize = 32;

/// `union i2c_smbus_data` from `linux/i2c.h`.
#[repr(C)]
union SmbusData {
    byte: u8,
    word: u16,
    /// Length byte, up to 32 data bytes, and one spare for PEC.
    block: [u8; BLOCK_MAX + 2],
}

/// `struct i2c_smbus_ioctl_data` from `linux/i2c-dev.h`.
#[repr(C)]
struct SmbusIoctl {
    read_write: u8,
    command: u8,
    size: u32,
    data: *mut SmbusData,
}

/// Adapter name prefixes that carry the DIMM bus, in preference order. A
/// desktop exposes many I2C adapters (GPU DDC, DesignWare, ...); the chipset
/// SMBus is the one its driver registers. On AMD the DIMMs sit on PIIX4
/// port 0.
const ADAPTER_PREFERENCE: [&str; 4] = [
    "SMBus PIIX4 adapter port 0",
    "SMBus PIIX4 adapter",
    "SMBus I801 adapter",
    "SMBus",
];

/// An I2C adapter the kernel knows.
#[derive(Clone, Debug)]
pub struct Adapter {
    pub number: u32,
    pub name: String,
    /// Its sysfs directory, e.g. `/sys/bus/i2c/devices/i2c-3`.
    pub sysfs: PathBuf,
}

/// Every I2C adapter, by ascending number.
pub fn adapters() -> Vec<Adapter> {
    let Ok(entries) = fs::read_dir("/sys/bus/i2c/devices") else {
        return Vec::new();
    };
    let mut found: Vec<Adapter> = entries
        .flatten()
        .filter_map(|entry| {
            let number = entry.file_name().to_str()?.strip_prefix("i2c-")?.parse().ok()?;
            let name = fs::read_to_string(entry.path().join("name")).ok()?.trim().to_string();
            Some(Adapter { number, name, sysfs: entry.path() })
        })
        .collect();
    found.sort_by_key(|a| a.number);
    found
}

/// The chipset SMBus adapter the memory sits on, if its driver is loaded.
pub fn chipset_adapter() -> Option<Adapter> {
    let all = adapters();
    ADAPTER_PREFERENCE
        .iter()
        .find_map(|prefix| all.iter().find(|a| a.name.starts_with(prefix)))
        .cloned()
}

pub struct LinuxSmBus {
    file: File,
    /// Address the kernel currently has selected on this descriptor.
    selected: Option<u8>,
}

impl LinuxSmBus {
    pub fn open(number: u32) -> Result<Self> {
        let path = format!("/dev/i2c-{number}");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|e| Error::Io(format!("{path}: {e}")))?;
        Ok(LinuxSmBus { file, selected: None })
    }

    fn transfer(&mut self, addr: u8, read_write: u8, command: u8, size: u32, data: &mut SmbusData) -> Result<()> {
        let fd = self.file.as_raw_fd();
        if self.selected != Some(addr) {
            // SAFETY: I2C_SLAVE takes the address by value
            if unsafe { libc::ioctl(fd, I2C_SLAVE as _, libc::c_ulong::from(addr)) } < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            self.selected = Some(addr);
        }
        let mut args = SmbusIoctl { read_write, command, size, data };
        // SAFETY: `args` and the buffer it points to outlive the call, and
        // both match the kernel's layout for this request
        if unsafe { libc::ioctl(fd, I2C_SMBUS as _, &mut args as *mut SmbusIoctl) } < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(())
    }
}

impl SmBus for LinuxSmBus {
    fn read_byte_data(&mut self, addr: u8, cmd: u8) -> Result<u8> {
        let mut data = SmbusData { block: [0; BLOCK_MAX + 2] };
        self.transfer(addr, SMBUS_READ, cmd, SIZE_BYTE_DATA, &mut data)?;
        // SAFETY: the kernel filled the byte member for a byte-data read
        Ok(unsafe { data.byte })
    }

    fn write_byte_data(&mut self, addr: u8, cmd: u8, value: u8) -> Result<()> {
        let mut data = SmbusData { block: [0; BLOCK_MAX + 2] };
        data.byte = value;
        self.transfer(addr, SMBUS_WRITE, cmd, SIZE_BYTE_DATA, &mut data)
    }

    fn write_word_data(&mut self, addr: u8, cmd: u8, value: u16) -> Result<()> {
        let mut data = SmbusData { block: [0; BLOCK_MAX + 2] };
        data.word = value;
        self.transfer(addr, SMBUS_WRITE, cmd, SIZE_WORD_DATA, &mut data)
    }

    fn write_block_data(&mut self, addr: u8, cmd: u8, bytes: &[u8]) -> Result<()> {
        if bytes.len() > BLOCK_MAX {
            return Err(Error::Io(format!("SMBus block of {} bytes exceeds {BLOCK_MAX}", bytes.len())));
        }
        let mut block = [0u8; BLOCK_MAX + 2];
        block[0] = bytes.len() as u8;
        block[1..=bytes.len()].copy_from_slice(bytes);
        let mut data = SmbusData { block };
        self.transfer(addr, SMBUS_WRITE, cmd, SIZE_BLOCK_DATA, &mut data)
    }
}
