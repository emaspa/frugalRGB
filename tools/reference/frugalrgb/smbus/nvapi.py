"""NvAPI I2C backend — provides SMBus-compatible access to GPU I2C bus via NVIDIA driver."""

import ctypes
import logging
import sys
from ctypes import POINTER, Structure, byref, c_int32, c_uint8, c_uint32, c_void_p

from .interface import SMBusInterface

log = logging.getLogger(__name__)

# NvAPI function IDs (from NVIDIA nvapi_interface.h / OpenRGB)
_ID_INITIALIZE = 0x0150E828
_ID_UNLOAD = 0xD22BDD7E
_ID_ENUM_PHYSICAL_GPUS = 0xE5AC921F
_ID_GPU_GET_FULL_NAME = 0xCEEE8E9F
_ID_GPU_GET_PCI_IDS = 0x2DDFB66E
_ID_I2C_READ_EX = 0x4D7B0709
_ID_I2C_WRITE_EX = 0x283AC65A

_MAX_PHYSICAL_GPUS = 64
_SHORT_STRING_MAX = 64
_I2C_SPEED_DEPRECATED = 0xFFFF
_I2C_SPEED_DEFAULT = 0


class _NvI2CInfoV3(Structure):
    """NV_I2C_INFO_V3 for NvAPI I2C operations (64-bit layout)."""
    _fields_ = [
        ("version", c_uint32),
        ("display_mask", c_uint32),
        ("is_ddc_port", c_uint8),
        ("i2c_dev_address", c_uint8),
        ("i2c_reg_address", POINTER(c_uint8)),
        ("reg_addr_size", c_uint32),
        ("data", POINTER(c_uint8)),
        ("size", c_uint32),
        ("i2c_speed", c_uint32),
        ("i2c_speed_khz", c_uint32),
        ("port_id", c_uint8),
        ("is_port_id_set", c_uint32),
    ]


_I2C_V3_VERSION = (3 << 16) | ctypes.sizeof(_NvI2CInfoV3)

# Function pointer types
_FN_STATUS = ctypes.CFUNCTYPE(c_int32)
_FN_ENUM_GPUS = ctypes.CFUNCTYPE(c_int32, POINTER(c_void_p), POINTER(c_int32))
_FN_GET_NAME = ctypes.CFUNCTYPE(c_int32, c_void_p, ctypes.c_char_p)
_FN_GET_PCI = ctypes.CFUNCTYPE(
    c_int32, c_void_p,
    POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint32),
)
_FN_I2C = ctypes.CFUNCTYPE(c_int32, c_void_p, POINTER(_NvI2CInfoV3), POINTER(c_uint32))


class NvAPIError(Exception):
    def __init__(self, func: str, status: int):
        super().__init__(f"{func} failed: 0x{status & 0xFFFFFFFF:08X}")
        self.status = status


class NvAPISession:
    """Manages NvAPI lifecycle: load, init, query functions, unload."""

    def __init__(self):
        self._dll = None
        self._qi = None
        self._unload = None
        self._enum_gpus = None
        self._get_name = None
        self._get_pci = None
        self._i2c_read = None
        self._i2c_write = None
        self._ready = False

    def open(self) -> None:
        dll_name = "nvapi64.dll" if sys.maxsize > 2**32 else "nvapi.dll"
        self._dll = ctypes.cdll.LoadLibrary(dll_name)

        qi = self._dll.nvapi_QueryInterface
        qi.restype = c_void_p
        qi.argtypes = [c_uint32]
        self._qi = qi

        init_fn = self._resolve(_ID_INITIALIZE, _FN_STATUS)
        self._unload = self._resolve(_ID_UNLOAD, _FN_STATUS)
        self._enum_gpus = self._resolve(_ID_ENUM_PHYSICAL_GPUS, _FN_ENUM_GPUS)
        self._get_name = self._resolve(_ID_GPU_GET_FULL_NAME, _FN_GET_NAME)
        self._get_pci = self._resolve(_ID_GPU_GET_PCI_IDS, _FN_GET_PCI)
        self._i2c_read = self._resolve(_ID_I2C_READ_EX, _FN_I2C)
        self._i2c_write = self._resolve(_ID_I2C_WRITE_EX, _FN_I2C)

        status = init_fn()
        if status != 0:
            raise NvAPIError("NvAPI_Initialize", status)
        self._ready = True
        log.info("NvAPI initialized")

    def _resolve(self, func_id, func_type):
        ptr = self._qi(func_id)
        if not ptr:
            raise RuntimeError(f"NvAPI QueryInterface(0x{func_id:08X}) returned NULL")
        return func_type(ptr)

    def close(self) -> None:
        if self._ready and self._unload:
            self._unload()
            self._ready = False

    def enum_gpus(self) -> list[tuple]:
        """Return list of (handle, name, vendor, device, subsys_vendor, subsys_device)."""
        handles = (c_void_p * _MAX_PHYSICAL_GPUS)()
        count = c_int32(0)
        status = self._enum_gpus(handles, byref(count))
        if status != 0:
            raise NvAPIError("NvAPI_EnumPhysicalGPUs", status)

        gpus = []
        for i in range(count.value):
            h = handles[i]
            name_buf = ctypes.create_string_buffer(_SHORT_STRING_MAX)
            if self._get_name(h, name_buf) == 0:
                name = name_buf.value.decode("utf-8", errors="replace")
            else:
                name = f"GPU {i}"

            did, ssid, rid, edid = c_uint32(), c_uint32(), c_uint32(), c_uint32()
            if self._get_pci(h, byref(did), byref(ssid), byref(rid), byref(edid)) == 0:
                vendor = did.value & 0xFFFF
                device = did.value >> 16
                sv = ssid.value & 0xFFFF
                sd = ssid.value >> 16
            else:
                vendor = device = sv = sd = 0

            gpus.append((h, name, vendor, device, sv, sd))
        return gpus

    def i2c_write(self, handle, addr7: int, reg: int, data: bytes, port: int = 1) -> None:
        reg_byte = c_uint8(reg)
        buf = (c_uint8 * len(data))(*data)
        info = _NvI2CInfoV3(
            version=_I2C_V3_VERSION, display_mask=0, is_ddc_port=0,
            i2c_dev_address=(addr7 << 1),
            i2c_reg_address=ctypes.pointer(reg_byte), reg_addr_size=1,
            data=buf, size=len(data),
            i2c_speed=_I2C_SPEED_DEPRECATED, i2c_speed_khz=_I2C_SPEED_DEFAULT,
            port_id=port, is_port_id_set=1,
        )
        unk = c_uint32(0)
        status = self._i2c_write(handle, byref(info), byref(unk))
        if status != 0:
            raise NvAPIError("NvAPI_I2CWriteEx", status)

    def i2c_read(self, handle, addr7: int, reg: int, length: int, port: int = 1) -> bytes:
        reg_byte = c_uint8(reg)
        buf = (c_uint8 * length)()
        info = _NvI2CInfoV3(
            version=_I2C_V3_VERSION, display_mask=0, is_ddc_port=0,
            i2c_dev_address=(addr7 << 1),
            i2c_reg_address=ctypes.pointer(reg_byte), reg_addr_size=1,
            data=buf, size=length,
            i2c_speed=_I2C_SPEED_DEPRECATED, i2c_speed_khz=_I2C_SPEED_DEFAULT,
            port_id=port, is_port_id_set=1,
        )
        unk = c_uint32(0)
        status = self._i2c_read(handle, byref(info), byref(unk))
        if status != 0:
            raise NvAPIError("NvAPI_I2CReadEx", status)
        return bytes(buf)


class NvAPII2CBus(SMBusInterface):
    """SMBus-compatible wrapper over NvAPI I2C for a specific GPU."""

    def __init__(self, session: NvAPISession, gpu_handle, port: int = 1):
        self._session = session
        self._handle = gpu_handle
        self._port = port

    def open(self) -> None:
        pass  # Session already open

    def close(self) -> None:
        pass  # Session manages lifecycle

    def read_byte_data(self, addr: int, cmd: int) -> int:
        data = self._session.i2c_read(self._handle, addr, cmd, 1, self._port)
        return data[0]

    def write_byte_data(self, addr: int, cmd: int, value: int) -> None:
        self._session.i2c_write(self._handle, addr, cmd, bytes([value]), self._port)

    def write_word_data(self, addr: int, cmd: int, value: int) -> None:
        lo = value & 0xFF
        hi = (value >> 8) & 0xFF
        self._session.i2c_write(self._handle, addr, cmd, bytes([lo, hi]), self._port)

    def write_block_data(self, addr: int, cmd: int, data: list[int]) -> None:
        self._session.i2c_write(self._handle, addr, cmd, bytes(data), self._port)
