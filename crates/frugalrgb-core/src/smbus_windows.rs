//! [`SmBus`] on Windows through the PawnIO driver (<https://pawnio.eu/>).
//!
//! PawnIO runs small signed modules in the kernel. `SmbusI801` drives the
//! Intel chipset SMBus and `SmbusPIIX4` the AMD one; both offer the same
//! `ioctl_smbus_xfer` call. The process must run as Administrator.

use std::ffi::c_void;
use std::path::PathBuf;
use std::thread::sleep;
use std::time::Duration;

use crate::transport::SmBus;
use crate::win::{wide, Library};
use crate::{Error, Result};

type Hresult = i32;
type OpenFn = unsafe extern "system" fn(*mut *mut c_void) -> Hresult;
type LoadFn = unsafe extern "system" fn(*mut c_void, *const u8, usize) -> Hresult;
type ExecuteFn =
    unsafe extern "system" fn(*mut c_void, *const u8, *const u64, usize, *mut u64, usize, *mut usize) -> Hresult;
type CloseFn = unsafe extern "system" fn(*mut c_void) -> Hresult;

#[link(name = "kernel32")]
extern "system" {
    fn CreateMutexW(attributes: *const c_void, initial_owner: i32, name: *const u16) -> *mut c_void;
    fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
    fn ReleaseMutex(handle: *mut c_void) -> i32;
    fn CloseHandle(handle: *mut c_void) -> i32;
}

const E_ACCESSDENIED: u32 = 0x8007_0005;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_ABANDONED: u32 = 0x80;

/// The mutex hardware monitors (FanControl, LibreHardwareMonitor, ...) take
/// around SMBus transfers, so tools sharing the bus do not interleave.
const SMBUS_MUTEX: &str = r"Global\Access_SMBUS.HTP.Method";

const SMBUS_WRITE: u64 = 0;
const SMBUS_READ: u64 = 1;
const BYTE_DATA: u64 = 2;
const WORD_DATA: u64 = 3;
const BLOCK_DATA: u64 = 5;
const BLOCK_MAX: usize = 32;

/// Other tools poll the bus too; a transfer that collides is tried again.
const ATTEMPTS: usize = 5;
const RETRY_DELAY: Duration = Duration::from_millis(10);

/// The kernel modules, in the order they are tried. A module only loads on
/// a chipset it knows. The copies built into the program are used unless a
/// `modules` folder next to the executable holds a replacement.
const MODULES: [(&str, &[u8]); 2] = [
    ("SmbusI801", include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../modules/SmbusI801.bin"))),
    ("SmbusPIIX4", include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../modules/SmbusPIIX4.bin"))),
];

/// Where PawnIO installs its user-mode library.
pub fn library_path() -> PathBuf {
    let programs = std::env::var_os("ProgramFiles").unwrap_or_else(|| r"C:\Program Files".into());
    PathBuf::from(programs).join("PawnIO").join("PawnIOLib.dll")
}

fn module_blob(name: &str, built_in: &'static [u8]) -> Vec<u8> {
    let replacement = std::env::current_exe()
        .ok()
        .and_then(|exe| Some(exe.parent()?.join("modules").join(format!("{name}.bin"))))
        .and_then(|path| std::fs::read(path).ok());
    replacement.unwrap_or_else(|| built_in.to_vec())
}

fn failed(what: &str, hr: Hresult) -> Error {
    Error::Io(format!("{what} failed: HRESULT 0x{:08X}", hr as u32))
}

pub struct PawnIoSmBus {
    handle: *mut c_void,
    execute: ExecuteFn,
    close: CloseFn,
    mutex: *mut c_void,
    module: &'static str,
}

// SAFETY: the executor handle and the mutex are process-wide kernel objects
// and every use goes through `&mut self`
unsafe impl Send for PawnIoSmBus {}

impl PawnIoSmBus {
    pub fn open() -> Result<Self> {
        let path = library_path();
        if !path.exists() {
            return Err(Error::Io(format!(
                "PawnIO is not installed ({} is missing). Get it from https://pawnio.eu/",
                path.display()
            )));
        }
        let missing = || Error::Io(format!("{} is not a usable PawnIO library", path.display()));
        let library = Library::load(&path).ok_or_else(missing)?;
        // SAFETY: the signatures are the ones in PawnIOLib.h
        let (open, load, execute, close) = unsafe {
            (
                std::mem::transmute::<*mut c_void, OpenFn>(library.symbol(c"pawnio_open").ok_or_else(missing)?),
                std::mem::transmute::<*mut c_void, LoadFn>(library.symbol(c"pawnio_load").ok_or_else(missing)?),
                std::mem::transmute::<*mut c_void, ExecuteFn>(
                    library.symbol(c"pawnio_execute").ok_or_else(missing)?,
                ),
                std::mem::transmute::<*mut c_void, CloseFn>(library.symbol(c"pawnio_close").ok_or_else(missing)?),
            )
        };

        let mut handle = std::ptr::null_mut();
        // SAFETY: `handle` is a valid place for the executor handle
        let hr = unsafe { open(&mut handle) };
        if hr != 0 {
            return Err(if hr as u32 == E_ACCESSDENIED {
                Error::Io("PawnIO refused access. Run frugalRGB as Administrator.".into())
            } else {
                failed("pawnio_open", hr)
            });
        }

        let mut bus = PawnIoSmBus { handle, execute, close, mutex: std::ptr::null_mut(), module: "" };
        let mut refusals = Vec::new();
        for (name, built_in) in MODULES {
            let blob = module_blob(name, built_in);
            // SAFETY: the blob outlives the call
            let hr = unsafe { load(bus.handle, blob.as_ptr(), blob.len()) };
            if hr == 0 {
                bus.module = name;
                break;
            }
            refusals.push(format!("{name}: 0x{:08X}", hr as u32));
        }
        if bus.module.is_empty() {
            // Dropping `bus` closes the executor
            return Err(Error::Io(format!(
                "no SMBus module fits this chipset or PawnIO version ({})",
                refusals.join(", ")
            )));
        }

        // [name, I/O base, PCI ids]; a module that cannot answer this found
        // no controller it can drive
        let identity = bus.call("ioctl_identity", &[], 3)?;
        log::info!(
            "SMBus through PawnIO module {}, controller at 0x{:04X}",
            bus.module,
            identity.get(1).copied().unwrap_or(0)
        );

        let name = wide(SMBUS_MUTEX);
        // SAFETY: `name` is NUL-terminated; a null handle just means no
        // coordination with other tools
        bus.mutex = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        Ok(bus)
    }

    /// The module that loaded: `SmbusI801` or `SmbusPIIX4`.
    pub fn module(&self) -> &str {
        self.module
    }

    fn call(&mut self, function: &str, input: &[u64], out_len: usize) -> Result<Vec<u64>> {
        let name = std::ffi::CString::new(function).map_err(|_| Error::Io("bad function name".into()))?;
        let mut out = vec![0u64; out_len.max(1)];
        let mut written = 0usize;
        // SAFETY: every pointer refers to a live buffer of the stated length
        let hr = unsafe {
            (self.execute)(
                self.handle,
                name.as_ptr().cast(),
                input.as_ptr(),
                input.len(),
                out.as_mut_ptr(),
                out_len,
                &mut written,
            )
        };
        if hr != 0 {
            return Err(failed(function, hr));
        }
        out.truncate(written.min(out.len()));
        Ok(out)
    }

    /// One SMBus transfer, holding the shared mutex, retried on collisions.
    fn transfer(&mut self, input: &[u64]) -> Result<u64> {
        let mut last = Error::Io("SMBus transfer was not attempted".into());
        for attempt in 0..ATTEMPTS {
            if attempt > 0 {
                sleep(RETRY_DELAY);
            }
            if !self.mutex.is_null() {
                // SAFETY: the mutex handle is valid until drop
                let wait = unsafe { WaitForSingleObject(self.mutex, 10_000) };
                if wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED {
                    return Err(Error::Io("another program is holding the SMBus".into()));
                }
            }
            let result = self.call("ioctl_smbus_xfer", input, 1);
            if !self.mutex.is_null() {
                // SAFETY: this thread owns the mutex since the wait above
                unsafe { ReleaseMutex(self.mutex) };
            }
            match result {
                Ok(out) => return Ok(out.first().copied().unwrap_or(0)),
                Err(e) => last = e,
            }
        }
        Err(last)
    }
}

impl Drop for PawnIoSmBus {
    fn drop(&mut self) {
        // SAFETY: both handles came from the matching create/open calls
        unsafe {
            if !self.mutex.is_null() {
                CloseHandle(self.mutex);
            }
            (self.close)(self.handle);
        }
    }
}

impl SmBus for PawnIoSmBus {
    fn read_byte_data(&mut self, addr: u8, cmd: u8) -> Result<u8> {
        let value = self.transfer(&[addr.into(), SMBUS_READ, cmd.into(), BYTE_DATA])?;
        Ok(value as u8)
    }

    fn write_byte_data(&mut self, addr: u8, cmd: u8, value: u8) -> Result<()> {
        self.transfer(&[addr.into(), SMBUS_WRITE, cmd.into(), BYTE_DATA, value.into()]).map(drop)
    }

    fn write_word_data(&mut self, addr: u8, cmd: u8, value: u16) -> Result<()> {
        self.transfer(&[addr.into(), SMBUS_WRITE, cmd.into(), WORD_DATA, value.into()]).map(drop)
    }

    fn write_block_data(&mut self, addr: u8, cmd: u8, data: &[u8]) -> Result<()> {
        if data.len() > BLOCK_MAX {
            return Err(Error::Io(format!("SMBus block of {} bytes exceeds {BLOCK_MAX}", data.len())));
        }
        let mut input = vec![addr.into(), SMBUS_WRITE, cmd.into(), BLOCK_DATA];
        input.extend(pack_block(data));
        self.transfer(&input).map(drop)
    }
}

/// The length byte and the data, eight bytes to a cell, lowest byte first.
fn pack_block(data: &[u8]) -> Vec<u64> {
    let mut bytes = vec![data.len() as u8];
    bytes.extend_from_slice(data);
    bytes
        .chunks(8)
        .map(|chunk| chunk.iter().enumerate().fold(0u64, |cell, (i, &b)| cell | u64::from(b) << (8 * i)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::pack_block;

    #[test]
    fn block_is_length_prefixed_and_little_endian() {
        assert_eq!(pack_block(&[0xAA]), [0xAA01]);
        assert_eq!(
            pack_block(&[1, 2, 3, 4, 5, 6, 7, 8, 9]),
            [0x0706_0504_0302_0109, 0x0908]
        );
    }
}
