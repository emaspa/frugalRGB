//! The few Win32 calls the Windows backends share.

use std::ffi::{c_void, CStr, OsStr};
use std::os::windows::ffi::OsStrExt;

#[link(name = "kernel32")]
extern "system" {
    fn LoadLibraryW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
}

#[link(name = "shell32")]
extern "system" {
    fn IsUserAnAdmin() -> i32;
}

/// `text` as a NUL-terminated UTF-16 string.
pub fn wide(text: impl AsRef<OsStr>) -> Vec<u16> {
    text.as_ref().encode_wide().chain(std::iter::once(0)).collect()
}

/// Whether the process runs elevated. Kernel-level SMBus access needs it.
pub fn is_admin() -> bool {
    // SAFETY: takes no arguments and only reads the process token
    unsafe { IsUserAnAdmin() != 0 }
}

/// A loaded DLL. It stays loaded for the life of the process: the function
/// pointers taken from it are kept by long-lived backends.
pub struct Library(*mut c_void);

impl Library {
    pub fn load(name: impl AsRef<OsStr>) -> Option<Self> {
        let name = wide(name);
        // SAFETY: `name` is NUL-terminated and outlives the call
        let module = unsafe { LoadLibraryW(name.as_ptr()) };
        (!module.is_null()).then_some(Library(module))
    }

    /// The address of an exported function, or `None` when it is missing.
    pub fn symbol(&self, name: &CStr) -> Option<*mut c_void> {
        // SAFETY: the module handle is valid and `name` is NUL-terminated
        let address = unsafe { GetProcAddress(self.0, name.as_ptr().cast()) };
        (!address.is_null()).then_some(address)
    }
}
