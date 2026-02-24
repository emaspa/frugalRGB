import ctypes
import os
import sys
import time

from .interface import SMBusInterface

UINT64 = ctypes.c_uint64
SIZE_T = ctypes.c_size_t
HANDLE = ctypes.c_void_p
PHANDLE = ctypes.POINTER(ctypes.c_void_p)
HRESULT = ctypes.c_long

# SMBus protocol constants (match PawnIO SmbusI801 module)
I2C_SMBUS_READ = 1
I2C_SMBUS_WRITE = 0
I2C_SMBUS_BYTE_DATA = 2
I2C_SMBUS_WORD_DATA = 3
I2C_SMBUS_BLOCK_DATA = 5

# Default paths
PAWNIO_DLL_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
if getattr(sys, "frozen", False):
    _EXE_DIR = os.path.dirname(sys.executable)
    # PyInstaller puts data in _internal/ next to the exe
    _INTERNAL = os.path.join(_EXE_DIR, "_internal")
    _BASE_DIR = _INTERNAL if os.path.isdir(os.path.join(_INTERNAL, "modules")) else _EXE_DIR
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
MODULE_DIR = os.path.join(_BASE_DIR, "modules")
SMBUS_MODULE_PATH = os.path.join(MODULE_DIR, "SmbusI801.bin")

# Windows mutex for SMBus access coordination
SMBUS_MUTEX_NAME = r"Global\Access_SMBUS.HTP.Method"


class PawnIOExecutor:
    """Manages a PawnIO executor with a loaded SmbusI801 module."""

    def __init__(self, dll_path: str = PAWNIO_DLL_PATH, module_path: str = SMBUS_MODULE_PATH):
        self._dll_path = dll_path
        self._module_path = module_path
        self._dll = None
        self._handle = HANDLE()
        self._mutex = None

    def open(self) -> None:
        if not os.path.exists(self._dll_path):
            raise RuntimeError(
                f"PawnIOLib.dll not found at {self._dll_path}\n"
                "Install PawnIO from https://pawnio.eu/"
            )
        self._dll = ctypes.CDLL(self._dll_path)
        self._setup_argtypes()

        # Open executor
        hr = self._dll.pawnio_open(ctypes.byref(self._handle))
        if hr != 0:
            if (hr & 0xFFFFFFFF) == 0x80070005:
                raise PermissionError(
                    "Access denied. Run frugalRGB as Administrator."
                )
            raise RuntimeError(f"pawnio_open failed: HRESULT=0x{hr & 0xFFFFFFFF:08X}")

        # Load SmbusI801 module
        if not os.path.exists(self._module_path):
            self._dll.pawnio_close(self._handle)
            raise FileNotFoundError(
                f"SmbusI801.bin not found at {self._module_path}\n"
                "Download from https://github.com/namazso/PawnIO.Modules/releases"
            )

        with open(self._module_path, "rb") as f:
            blob = f.read()

        hr = self._dll.pawnio_load(self._handle, ctypes.create_string_buffer(blob), len(blob))
        if hr != 0:
            self._dll.pawnio_close(self._handle)
            raise RuntimeError(
                f"Failed to load SmbusI801 module: HRESULT=0x{hr & 0xFFFFFFFF:08X}\n"
                "The module may be incompatible with your PawnIO version."
            )

        # Acquire SMBus mutex (shared with FanControl, LHM, etc.)
        kernel32 = ctypes.windll.kernel32
        self._mutex = kernel32.CreateMutexW(None, False, SMBUS_MUTEX_NAME)

    def _setup_argtypes(self) -> None:
        """Declare proper ctypes argtypes for 64-bit correctness."""
        self._dll.pawnio_version.argtypes = [ctypes.POINTER(ctypes.c_ulong)]
        self._dll.pawnio_version.restype = HRESULT
        self._dll.pawnio_open.argtypes = [PHANDLE]
        self._dll.pawnio_open.restype = HRESULT
        self._dll.pawnio_load.argtypes = [HANDLE, ctypes.c_char_p, SIZE_T]
        self._dll.pawnio_load.restype = HRESULT
        self._dll.pawnio_execute.argtypes = [
            HANDLE, ctypes.c_char_p, ctypes.POINTER(UINT64),
            SIZE_T, ctypes.POINTER(UINT64), SIZE_T, ctypes.POINTER(SIZE_T),
        ]
        self._dll.pawnio_execute.restype = HRESULT
        self._dll.pawnio_close.argtypes = [HANDLE]
        self._dll.pawnio_close.restype = HRESULT

    def close(self) -> None:
        if self._mutex is not None:
            ctypes.windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None
        if self._dll is not None and self._handle:
            self._dll.pawnio_close(self._handle)
            self._handle = HANDLE()
            self._dll = None

    def execute(self, func_name: str, inputs: list[int] | None = None,
                out_size: int = 1) -> list[int]:
        """Call a function on the loaded PawnIO module.

        out_size must match what the module's DEFINE_IOCTL_SIZED expects.
        """
        if inputs is None:
            inputs = []
        in_count = len(inputs)
        in_buf = (UINT64 * in_count)(*inputs) if in_count > 0 else None
        out_buf = (UINT64 * max(out_size, 1))()
        out_len = SIZE_T(0)

        hr = self._dll.pawnio_execute(
            self._handle,
            func_name.encode("ascii"),
            in_buf, in_count,
            out_buf, out_size,
            ctypes.byref(out_len),
        )
        if hr != 0:
            raise IOError(
                f"pawnio_execute('{func_name}') failed: HRESULT=0x{hr & 0xFFFFFFFF:08X}"
            )

        return [out_buf[i] for i in range(out_len.value)]

    def lock_smbus(self) -> None:
        if self._mutex:
            ret = ctypes.windll.kernel32.WaitForSingleObject(self._mutex, 10000)
            if ret != 0:
                raise TimeoutError("Could not acquire SMBus mutex")

    def unlock_smbus(self) -> None:
        if self._mutex:
            ctypes.windll.kernel32.ReleaseMutex(self._mutex)


class WindowsSMBus(SMBusInterface):
    """Windows SMBus backend using PawnIO's SmbusI801 kernel module."""

    def __init__(self, bus_number: int = 0):
        self._bus_number = bus_number
        self._executor = PawnIOExecutor()

    def open(self) -> None:
        self._executor.open()

        # Verify the i801 controller was detected (out_size must be exactly 3)
        try:
            identity = self._executor.execute("ioctl_identity", out_size=3)
            smba = identity[1] if len(identity) > 1 else 0
            import logging
            logging.getLogger(__name__).info("i801 SMBus controller found, base=0x%04X", smba)
        except IOError as e:
            raise RuntimeError(
                "SmbusI801 module loaded but could not query controller.\n"
                f"Detail: {e}"
            )

    def close(self) -> None:
        self._executor.close()

    def _smbus_xfer(self, addr: int, read_write: int, command: int,
                    protocol: int, data: int = 0) -> int:
        """Execute a simple SMBus transfer (byte/word) with retries.

        Returns the result value (for reads) or 0 (for writes).
        Retries up to 5 times with 10ms delay to handle bus contention
        from other tools (FanControl, LHM) polling the SMBus.
        """
        inputs = [addr, read_write, command, protocol]
        if read_write == I2C_SMBUS_WRITE or data != 0:
            inputs.append(data)

        last_err = None
        for attempt in range(5):
            self._executor.lock_smbus()
            try:
                result = self._executor.execute("ioctl_smbus_xfer", inputs, out_size=1)
                return result[0] if result else 0
            except IOError as e:
                last_err = e
            finally:
                self._executor.unlock_smbus()
            time.sleep(0.01)

        raise last_err

    def read_byte_data(self, addr: int, cmd: int) -> int:
        val = self._smbus_xfer(addr, I2C_SMBUS_READ, cmd, I2C_SMBUS_BYTE_DATA)
        return val & 0xFF

    def write_byte_data(self, addr: int, cmd: int, value: int) -> None:
        self._smbus_xfer(addr, I2C_SMBUS_WRITE, cmd, I2C_SMBUS_BYTE_DATA, value)

    def write_word_data(self, addr: int, cmd: int, value: int) -> None:
        self._smbus_xfer(addr, I2C_SMBUS_WRITE, cmd, I2C_SMBUS_WORD_DATA, value)

    def write_block_data(self, addr: int, cmd: int, data: list[int]) -> None:
        if len(data) > 32:
            raise ValueError("SMBus block data max 32 bytes")

        # Pack: first byte is length, then data bytes, into UINT64 cells
        block = [len(data)] + data
        data_words = []
        for i in range(0, len(block), 8):
            word = 0
            for j in range(min(8, len(block) - i)):
                word |= (block[i + j] & 0xFF) << (j * 8)
            data_words.append(word)

        inputs = [addr, I2C_SMBUS_WRITE, cmd, I2C_SMBUS_BLOCK_DATA] + data_words
        last_err = None
        for attempt in range(5):
            self._executor.lock_smbus()
            try:
                self._executor.execute("ioctl_smbus_xfer", inputs, out_size=1)
                return
            except IOError as e:
                last_err = e
            finally:
                self._executor.unlock_smbus()
            time.sleep(0.01)
        raise last_err
