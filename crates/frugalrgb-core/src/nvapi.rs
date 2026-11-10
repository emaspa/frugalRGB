//! The I2C bus of an NVIDIA graphics card on Windows, through the driver's
//! NvAPI. ASUS cards keep their RGB chip on port 1 of it.

use std::ffi::c_void;
use std::sync::Arc;

use crate::transport::SmBus;
use crate::win::Library;
use crate::{Error, Result};

// Function ids for nvapi_QueryInterface, from NVIDIA's nvapi_interface.h
const ID_INITIALIZE: u32 = 0x0150_E828;
const ID_UNLOAD: u32 = 0xD22B_DD7E;
const ID_ENUM_PHYSICAL_GPUS: u32 = 0xE5AC_921F;
const ID_GPU_GET_FULL_NAME: u32 = 0xCEEE_8E9F;
const ID_GPU_GET_PCI_IDS: u32 = 0x2DDF_B66E;
const ID_I2C_READ_EX: u32 = 0x4D7B_0709;
const ID_I2C_WRITE_EX: u32 = 0x283A_C65A;

const MAX_PHYSICAL_GPUS: usize = 64;
const SHORT_STRING_MAX: usize = 64;
const I2C_SPEED_DEPRECATED: u32 = 0xFFFF;

type QueryFn = unsafe extern "C" fn(u32) -> *mut c_void;
type StatusFn = unsafe extern "C" fn() -> i32;
type EnumGpusFn = unsafe extern "C" fn(*mut *mut c_void, *mut i32) -> i32;
type GetNameFn = unsafe extern "C" fn(*mut c_void, *mut u8) -> i32;
type GetPciFn = unsafe extern "C" fn(*mut c_void, *mut u32, *mut u32, *mut u32, *mut u32) -> i32;
type I2cFn = unsafe extern "C" fn(*mut c_void, *mut I2cInfo, *mut u32) -> i32;

/// `NV_I2C_INFO_V3`.
#[repr(C)]
struct I2cInfo {
    version: u32,
    display_mask: u32,
    is_ddc_port: u8,
    /// 8-bit address: the 7-bit one shifted left.
    dev_address: u8,
    reg_address: *mut u8,
    reg_addr_size: u32,
    data: *mut u8,
    size: u32,
    speed: u32,
    speed_khz: u32,
    port_id: u8,
    is_port_id_set: u32,
}

const I2C_INFO_VERSION: u32 = (3 << 16) | std::mem::size_of::<I2cInfo>() as u32;

fn failed(what: &str, status: i32) -> Error {
    Error::Io(format!("{what} failed: 0x{:08X}", status as u32))
}

/// A physical NVIDIA GPU.
pub struct Gpu {
    handle: *mut c_void,
    pub name: String,
    pub subsystem_vendor: u16,
    pub subsystem_device: u16,
}

/// An initialised NvAPI. Unloads when the last user drops it.
pub struct NvApi {
    unload: StatusFn,
    enum_gpus: EnumGpusFn,
    get_name: GetNameFn,
    get_pci: GetPciFn,
    i2c_read: I2cFn,
    i2c_write: I2cFn,
}

// SAFETY: NvAPI is initialised once per process and its calls may come from
// any thread; the struct only holds function pointers
unsafe impl Send for NvApi {}
unsafe impl Sync for NvApi {}

impl NvApi {
    /// Load and initialise NvAPI. Fails on machines without NVIDIA drivers.
    pub fn open() -> Result<Arc<Self>> {
        let dll = if cfg!(target_pointer_width = "64") { "nvapi64.dll" } else { "nvapi.dll" };
        let library = Library::load(dll).ok_or_else(|| Error::Io(format!("{dll} is not available")))?;
        let query = library
            .symbol(c"nvapi_QueryInterface")
            .ok_or_else(|| Error::Io(format!("{dll} has no nvapi_QueryInterface")))?;
        // SAFETY: the documented signature of the one exported entry point
        let query = unsafe { std::mem::transmute::<*mut c_void, QueryFn>(query) };
        let resolve = |id: u32| -> Result<*mut c_void> {
            // SAFETY: takes an id, returns a function address or null
            let address = unsafe { query(id) };
            if address.is_null() {
                return Err(Error::Io(format!("NvAPI has no function 0x{id:08X}")));
            }
            Ok(address)
        };

        // SAFETY: each id is paired with the signature NvAPI documents for it
        let (initialize, api) = unsafe {
            (
                std::mem::transmute::<*mut c_void, StatusFn>(resolve(ID_INITIALIZE)?),
                NvApi {
                    unload: std::mem::transmute::<*mut c_void, StatusFn>(resolve(ID_UNLOAD)?),
                    enum_gpus: std::mem::transmute::<*mut c_void, EnumGpusFn>(resolve(ID_ENUM_PHYSICAL_GPUS)?),
                    get_name: std::mem::transmute::<*mut c_void, GetNameFn>(resolve(ID_GPU_GET_FULL_NAME)?),
                    get_pci: std::mem::transmute::<*mut c_void, GetPciFn>(resolve(ID_GPU_GET_PCI_IDS)?),
                    i2c_read: std::mem::transmute::<*mut c_void, I2cFn>(resolve(ID_I2C_READ_EX)?),
                    i2c_write: std::mem::transmute::<*mut c_void, I2cFn>(resolve(ID_I2C_WRITE_EX)?),
                },
            )
        };
        // SAFETY: no arguments
        let status = unsafe { initialize() };
        if status != 0 {
            // Not initialised, so there is nothing to unload
            std::mem::forget(api);
            return Err(failed("NvAPI_Initialize", status));
        }
        log::debug!("NvAPI initialized");
        Ok(Arc::new(api))
    }

    pub fn gpus(&self) -> Result<Vec<Gpu>> {
        let mut handles = [std::ptr::null_mut::<c_void>(); MAX_PHYSICAL_GPUS];
        let mut count = 0i32;
        // SAFETY: the array has the 64 slots NvAPI may fill
        let status = unsafe { (self.enum_gpus)(handles.as_mut_ptr(), &mut count) };
        if status != 0 {
            return Err(failed("NvAPI_EnumPhysicalGPUs", status));
        }

        let mut gpus = Vec::new();
        for (index, &handle) in handles.iter().take(count.max(0) as usize).enumerate() {
            let mut text = [0u8; SHORT_STRING_MAX];
            // SAFETY: the buffer is an NvAPI_ShortString
            let name = if unsafe { (self.get_name)(handle, text.as_mut_ptr()) } == 0 {
                let end = text.iter().position(|&b| b == 0).unwrap_or(text.len());
                String::from_utf8_lossy(&text[..end]).into_owned()
            } else {
                format!("GPU {index}")
            };

            let (mut device, mut subsystem, mut revision, mut external) = (0u32, 0u32, 0u32, 0u32);
            // SAFETY: four valid out pointers
            let known =
                unsafe { (self.get_pci)(handle, &mut device, &mut subsystem, &mut revision, &mut external) } == 0;
            // The subsystem id is device << 16 | vendor
            let (subsystem_vendor, subsystem_device) =
                if known { (subsystem as u16, (subsystem >> 16) as u16) } else { (0, 0) };
            gpus.push(Gpu { handle, name, subsystem_vendor, subsystem_device });
        }
        Ok(gpus)
    }

    fn transfer(&self, call: I2cFn, what: &str, gpu: *mut c_void, port: u8, addr: u8, reg: u8, data: &mut [u8]) -> Result<()> {
        let mut reg = reg;
        let mut info = I2cInfo {
            version: I2C_INFO_VERSION,
            display_mask: 0,
            is_ddc_port: 0,
            dev_address: addr << 1,
            reg_address: &mut reg,
            reg_addr_size: 1,
            data: data.as_mut_ptr(),
            size: data.len() as u32,
            speed: I2C_SPEED_DEPRECATED,
            speed_khz: 0,
            port_id: port,
            is_port_id_set: 1,
        };
        let mut unknown = 0u32;
        // SAFETY: `info` and the buffers it points to outlive the call
        let status = unsafe { call(gpu, &mut info, &mut unknown) };
        if status != 0 {
            return Err(failed(what, status));
        }
        Ok(())
    }
}

impl Drop for NvApi {
    fn drop(&mut self) {
        // SAFETY: NvAPI was initialised when this value was created
        unsafe { (self.unload)() };
    }
}

/// One I2C port of a GPU as an SMBus.
pub struct NvI2c {
    api: Arc<NvApi>,
    gpu: *mut c_void,
    port: u8,
}

// SAFETY: a GPU handle is an opaque id that NvAPI accepts on any thread
unsafe impl Send for NvI2c {}

impl NvI2c {
    pub fn new(api: Arc<NvApi>, gpu: &Gpu, port: u8) -> Self {
        NvI2c { api, gpu: gpu.handle, port }
    }

    fn write(&mut self, addr: u8, cmd: u8, data: &[u8]) -> Result<()> {
        let mut data = data.to_vec();
        self.api.transfer(self.api.i2c_write, "NvAPI_I2CWriteEx", self.gpu, self.port, addr, cmd, &mut data)
    }
}

impl SmBus for NvI2c {
    fn read_byte_data(&mut self, addr: u8, cmd: u8) -> Result<u8> {
        let mut data = [0u8];
        self.api.transfer(self.api.i2c_read, "NvAPI_I2CReadEx", self.gpu, self.port, addr, cmd, &mut data)?;
        Ok(data[0])
    }

    fn write_byte_data(&mut self, addr: u8, cmd: u8, value: u8) -> Result<()> {
        self.write(addr, cmd, &[value])
    }

    fn write_word_data(&mut self, addr: u8, cmd: u8, value: u16) -> Result<()> {
        self.write(addr, cmd, &value.to_le_bytes())
    }

    fn write_block_data(&mut self, addr: u8, cmd: u8, data: &[u8]) -> Result<()> {
        self.write(addr, cmd, data)
    }
}
