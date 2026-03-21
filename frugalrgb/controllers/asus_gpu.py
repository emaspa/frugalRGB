"""ASUS GPU RGB controller — ENE microcontroller on GPU I2C bus via NvAPI."""

import logging
import sys

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

# ENE I2C address on ASUS GPUs (RTX 30xx+)
ENE_ADDR = 0x67

# ENE 16-bit register map (same as DDR5, accessed via GPU I2C)
REG_DEVICE_NAME = 0x1000
REG_CONFIG_TABLE = 0x1C00
REG_DIRECT_SELECT = 0x8020
REG_MODE = 0x8021
REG_SPEED = 0x8022
REG_DIRECTION = 0x8023
REG_V2_EFFECT_COLOR = 0x8160
REG_APPLY = 0x80A0

APPLY_VAL = 0x01
SAVE_VAL = 0xAA

# ASUS subsystem vendor
ASUS_SUB_VEN = 0x1043

# Known ASUS GPU subsystem device IDs
KNOWN_SUBSYS = {
    # TUF RTX 50-series
    0x89EE: "TUF RTX 5090 OC",
    0x89EF: "TUF RTX 5090",
    0x89D7: "TUF RTX 5080 OC",
    0x89F4: "TUF RTX 5070 Ti OC",
    0x89F2: "TUF RTX 5070",
    # ROG RTX 50-series
    0x89E3: "ROG ASTRAL RTX 5090 OC",
    0x8A3C: "ROG ASTRAL RTX 5090 OC BTF",
    0x8A2E: "ROG ASTRAL RTX 5090 OC WHITE",
    0x8A61: "ROG MATRIX PLATINUM RTX 5090",
    # TUF RTX 40-series
    0x8919: "TUF RTX 4090 OC",
    0x891A: "TUF RTX 4090",
    0x88F3: "TUF RTX 4080 OC",
    0x88F4: "TUF RTX 4080",
    0x88C7: "TUF RTX 4070 Ti OC",
}

# Mode mapping (same as ENE DDR5)
ENE_MODE_MAP = {
    RGBMode.OFF: 0x00,
    RGBMode.STATIC: 0x01,
    RGBMode.BREATHING: 0x02,
    RGBMode.STROBE: 0x03,
    RGBMode.COLOR_CYCLE: 0x04,
    RGBMode.RAINBOW: 0x05,
}

ENE_SPEED_MAP = {0: 0x00, 1: 0x01, 2: 0x02, 3: 0x03, 4: 0x04, 5: 0x04}


def _swap16(val: int) -> int:
    return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)


class ASUSGPUController(RGBController):
    """Controller for ASUS GPU RGB via ENE on GPU I2C bus."""

    def __init__(self, bus, gpu_name: str, subsys_name: str, device_name: str):
        self._bus = bus
        self._gpu_name = gpu_name
        self._subsys_name = subsys_name
        self._device_name = device_name
        self._num_leds = 1
        self._current_mode = RGBMode.STATIC
        self._color_correction = (1.0, 1.0, 1.0)

        # Read LED count from config table
        led_count = self._read_register(REG_CONFIG_TABLE + 0x03)
        if led_count is not None and 1 <= led_count <= 32:
            self._num_leds = led_count
        log.info("ASUS GPU RGB: %d LED(s), ENE: %s", self._num_leds, device_name)

        # Build zones
        if self._num_leds == 1:
            self._zones = [RGBZone(0, "GPU")]
        else:
            self._zones = [RGBZone(0, "All LEDs")]
            for i in range(self._num_leds):
                self._zones.append(RGBZone(i + 1, f"LED {i + 1}"))

        # Set effect mode
        self._write_register(REG_DIRECT_SELECT, 0x00)

    def _read_register(self, reg: int) -> int | None:
        try:
            self._bus.write_word_data(ENE_ADDR, 0x00, _swap16(reg))
            return self._bus.read_byte_data(ENE_ADDR, 0x81)
        except Exception:
            return None

    def _write_register(self, reg: int, value: int) -> None:
        self._bus.write_word_data(ENE_ADDR, 0x00, _swap16(reg))
        self._bus.write_byte_data(ENE_ADDR, 0x01, value)

    def _write_led_color(self, led_index: int, r: int, g: int, b: int) -> None:
        """Write color to a single LED (R, B, G order — ENE GPU format)."""
        base = REG_V2_EFFECT_COLOR + led_index * 3
        self._write_register(base, r)
        self._write_register(base + 1, b)
        self._write_register(base + 2, g)

    @property
    def name(self) -> str:
        if self._subsys_name:
            return f"ASUS {self._subsys_name}"
        return f"ASUS GPU RGB ({self._gpu_name})"

    @property
    def zones(self) -> list[RGBZone]:
        return self._zones

    @property
    def supported_modes(self) -> list[RGBMode]:
        return [RGBMode.OFF, RGBMode.STATIC, RGBMode.BREATHING,
                RGBMode.COLOR_CYCLE, RGBMode.RAINBOW, RGBMode.STROBE]

    @property
    def has_hardware_mode(self) -> bool:
        return self._current_mode in {
            RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
        }

    @property
    def color_correction(self) -> tuple[float, float, float]:
        return self._color_correction

    @color_correction.setter
    def color_correction(self, value: tuple[float, float, float]) -> None:
        self._color_correction = value

    def _correct_color(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        cr, cg, cb = self._color_correction
        return min(255, int(r * cr)), min(255, int(g * cg)), min(255, int(b * cb))

    def set_color(self, r: int, g: int, b: int, zone: int | None = None) -> None:
        r, g, b = self._correct_color(r, g, b)
        if zone is None or zone == 0:
            for i in range(self._num_leds):
                self._write_led_color(i, r, g, b)
        else:
            led_index = zone - 1
            if 0 <= led_index < self._num_leds:
                self._write_led_color(led_index, r, g, b)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        ene_mode = ENE_MODE_MAP.get(mode, 0x01)
        ene_speed = ENE_SPEED_MAP.get(speed, 0x03)
        self._write_register(REG_DIRECT_SELECT, 0x00)
        self._write_register(REG_MODE, ene_mode)
        self._write_register(REG_SPEED, ene_speed)
        self._write_register(REG_DIRECTION, 0x00)

    def apply(self) -> None:
        self._write_register(REG_APPLY, APPLY_VAL)

    @property
    def supports_hardware_save(self) -> bool:
        return True

    def save_to_hardware(self) -> None:
        self._write_register(REG_APPLY, SAVE_VAL)
        log.info("Saved settings to NV flash on %s", self.name)

    def close(self) -> None:
        pass


def _read_ene_name(bus, addr: int) -> str | None:
    """Read ENE device name string at register 0x1000."""
    chars = []
    try:
        for i in range(16):
            bus.write_word_data(addr, 0x00, _swap16(REG_DEVICE_NAME + i))
            val = bus.read_byte_data(addr, 0x81)
            if val == 0:
                break
            if 0x20 <= val < 0x7F:
                chars.append(chr(val))
            else:
                return None
    except Exception:
        return None
    return "".join(chars) if len(chars) >= 4 else None


def detect_asus_gpu() -> ASUSGPUController | None:
    """Detect ASUS GPU RGB controller via NvAPI I2C."""
    if sys.platform != "win32":
        return None

    try:
        from ..smbus.nvapi import NvAPISession, NvAPII2CBus
    except Exception as e:
        log.debug("NvAPI not available: %s", e)
        return None

    session = NvAPISession()
    try:
        session.open()
    except Exception as e:
        log.debug("NvAPI init failed: %s", e)
        return None

    try:
        gpus = session.enum_gpus()
    except Exception as e:
        log.debug("NvAPI GPU enumeration failed: %s", e)
        session.close()
        return None

    for handle, name, vendor, device, subsys_vendor, subsys_device in gpus:
        if subsys_vendor != ASUS_SUB_VEN:
            continue

        subsys_name = KNOWN_SUBSYS.get(subsys_device, "")
        log.info("ASUS GPU found: %s (subsys 0x%04X:0x%04X) %s",
                 name, subsys_vendor, subsys_device, subsys_name)

        bus = NvAPII2CBus(session, handle, port=1)

        # Probe ENE controller at 0x67
        ene_name = _read_ene_name(bus, ENE_ADDR)
        if ene_name is None:
            log.debug("No ENE controller at 0x%02X on %s", ENE_ADDR, name)
            continue

        log.info("Found ENE GPU RGB controller: %s", ene_name)
        return ASUSGPUController(bus, name, subsys_name, ene_name)

    session.close()
    return None
