import logging

from ..smbus.interface import SMBusInterface
from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

# ENE register addresses (16-bit, accessed via word writes to reg 0x00)
ENE_REG_DEVICE_NAME = 0x1000
ENE_REG_MICRON_CHECK = 0x1030
ENE_REG_CONFIG_TABLE = 0x1C00

ENE_REG_DIRECT_COLOR = 0x8000
ENE_REG_EFFECT_COLOR = 0x8010
ENE_REG_DIRECT_SELECT = 0x8020
ENE_REG_MODE = 0x8021
ENE_REG_SPEED = 0x8022
ENE_REG_DIRECTION = 0x8023
ENE_REG_APPLY = 0x80A0
ENE_REG_SLOT_INDEX = 0x80F8
ENE_REG_I2C_ADDR = 0x80F9

# Alternative register set (v2)
ENE_REG_DIRECT_COLOR_V2 = 0x8100
ENE_REG_EFFECT_COLOR_V2 = 0x8160

ENE_APPLY_VAL = 0x01
ENE_SAVE_VAL = 0xAA

# ENE mode values
ENE_MODE_MAP = {
    RGBMode.OFF: 0x00,
    RGBMode.STATIC: 0x01,
    RGBMode.BREATHING: 0x02,
    RGBMode.COLOR_CYCLE: 0x04,
    RGBMode.RAINBOW: 0x05,
    RGBMode.STROBE: 0x03,
}

# Speed: 0x00 = fast, 0x04 = slow
ENE_SPEED_MAP = {0: 0x00, 1: 0x01, 2: 0x02, 3: 0x03, 4: 0x04, 5: 0x04}


def _swap16(val: int) -> int:
    """Byte-swap a 16-bit value for ENE's register addressing."""
    return ((val << 8) & 0xFF00) | ((val >> 8) & 0x00FF)


class ENEDramController(RGBController):
    """Controller for ENE-based DRAM RGB (Klevv DDR5, G.Skill, etc.)."""

    def __init__(self, bus: SMBusInterface, addr: int, num_leds: int = 8):
        self._bus = bus
        self._addr = addr
        self._num_leds = num_leds
        self._current_mode = RGBMode.STATIC
        self._zones = [RGBZone(0, "DRAM")]

    @property
    def name(self) -> str:
        return f"ENE DRAM (0x{self._addr:02X})"

    @property
    def zones(self) -> list[RGBZone]:
        return self._zones

    @property
    def supported_modes(self) -> list[RGBMode]:
        return [RGBMode.OFF, RGBMode.STATIC, RGBMode.BREATHING,
                RGBMode.COLOR_CYCLE, RGBMode.RAINBOW, RGBMode.STROBE]

    def _set_register(self, reg: int) -> None:
        """Set the 16-bit register address for subsequent reads/writes."""
        self._bus.write_word_data(self._addr, 0x00, _swap16(reg))

    def _read_register(self, reg: int) -> int:
        """Read a byte from a 16-bit register address."""
        self._set_register(reg)
        return self._bus.read_byte_data(self._addr, 0x81)

    def _write_register(self, reg: int, value: int) -> None:
        """Write a byte to a 16-bit register address."""
        self._set_register(reg)
        self._bus.write_byte_data(self._addr, 0x01, value)

    def set_color(self, r: int, g: int, b: int, zone: int | None = None) -> None:
        # ENE uses BGR byte ordering
        color_data = []
        for _ in range(self._num_leds):
            color_data.extend([r, b, g])  # BGR ordering

        # Write color data in chunks (max 32 bytes per SMBus block)
        base_reg = ENE_REG_DIRECT_COLOR
        chunk_size = 30  # Must be multiple of 3
        for offset in range(0, len(color_data), chunk_size):
            chunk = color_data[offset:offset + chunk_size]
            self._set_register(base_reg + offset)
            for i, byte in enumerate(chunk):
                self._bus.write_byte_data(self._addr, 0x01, byte)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        ene_mode = ENE_MODE_MAP.get(mode, 0x01)
        ene_speed = ENE_SPEED_MAP.get(speed, 0x03)

        self._write_register(ENE_REG_DIRECT_SELECT, 0x00)  # Effect mode
        self._write_register(ENE_REG_MODE, ene_mode)
        self._write_register(ENE_REG_SPEED, ene_speed)

    def apply(self) -> None:
        self._write_register(ENE_REG_APPLY, ENE_APPLY_VAL)


def detect_ene_dram(bus: SMBusInterface) -> list[ENEDramController]:
    """Detect ENE DRAM RGB controllers on the SMBus.

    Uses 3-stage verification:
    1. Check if device responds at address
    2. Read registers 0xA0-0xAF — must contain incrementing 0x00-0x0F
    3. Verify it's not a Micron DRAM (which uses similar addresses)
    """
    controllers = []

    for addr in range(0x70, 0x78):
        try:
            # Stage 1: probe — does the device respond?
            bus.write_word_data(addr, 0x00, _swap16(0x00A0))
            test_val = bus.read_byte_data(addr, 0x81)

            # Stage 2: verify incrementing pattern at 0xA0-0xAF
            valid = True
            for i in range(16):
                bus.write_word_data(addr, 0x00, _swap16(0x00A0 + i))
                val = bus.read_byte_data(addr, 0x81)
                if val != i:
                    valid = False
                    break

            if not valid:
                continue

            # Stage 3: check for Micron (reject if found)
            bus.write_word_data(addr, 0x00, _swap16(ENE_REG_MICRON_CHECK))
            micron_data = []
            for i in range(6):
                bus.write_word_data(addr, 0x00, _swap16(ENE_REG_MICRON_CHECK + i))
                micron_data.append(bus.read_byte_data(addr, 0x81))

            if bytes(micron_data) == b"Micron":
                log.info("Skipping Micron DRAM at 0x%02X", addr)
                continue

            log.info("Found ENE DRAM controller at 0x%02X", addr)
            controllers.append(ENEDramController(bus, addr))

        except (IOError, OSError, TimeoutError):
            continue

    return controllers
