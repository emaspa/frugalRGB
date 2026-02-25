import logging

from ..smbus.interface import SMBusInterface
from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

# ENE AUDA0 DDR5 register map (16-bit addressing via word write to 0x00, read from 0x81)
REG_DEVICE_NAME = 0x1000
REG_CONFIG_TABLE = 0x1C00
REG_DIRECT_SELECT = 0x8020  # 0x00 = effect mode, 0x01 = direct mode
REG_MODE = 0x8021
REG_SPEED = 0x8022
REG_DIRECTION = 0x8023  # 0x00 or 0x01 — controls rainbow/wave direction
REG_V2_EFFECT_COLOR = 0x8160  # RGB triplets, 3 bytes per LED
REG_APPLY = 0x80A0

APPLY_VAL = 0x01
SAVE_VAL = 0xAA
NUM_LEDS = 8  # DDR5 sticks typically have 8 LEDs

# Mode mapping
ENE_DDR5_MODE_MAP = {
    RGBMode.OFF: 0x00,
    RGBMode.STATIC: 0x01,
    RGBMode.BREATHING: 0x02,
    RGBMode.STROBE: 0x03,
    RGBMode.COLOR_CYCLE: 0x04,
    RGBMode.RAINBOW: 0x05,
}

ENE_DDR5_SPEED_MAP = {0: 0x00, 1: 0x01, 2: 0x02, 3: 0x03, 4: 0x04, 5: 0x04}

# Known DDR5 RGB controller base addresses (slot index = addr - 0x70)
DDR5_ADDRS = range(0x70, 0x78)


def _swap16(val: int) -> int:
    return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)


class ENEDDR5Controller(RGBController):
    """Controller for ENE AUDA-series DDR5 DRAM RGB."""

    def __init__(self, bus: SMBusInterface, addr: int, device_name: str,
                 direction: int = 0x00):
        self._bus = bus
        self._addr = addr
        self._device_name = device_name
        self._num_leds = NUM_LEDS
        self._current_mode = RGBMode.STATIC
        self._color_correction = (1.0, 1.0, 1.0)
        self._direction = direction

        # Read LED count from config table
        led_count = self._read_register(REG_CONFIG_TABLE + 2)
        if led_count is not None and 1 <= led_count <= 32:
            self._num_leds = led_count
            log.debug("LED count from config: %d", self._num_leds)

        # Build zones: "All LEDs" + one per individual LED
        self._zones = [RGBZone(0, "All LEDs")]
        for i in range(self._num_leds):
            self._zones.append(RGBZone(i + 1, f"LED {i + 1}"))

        # Set effect mode (required for V2 effect color registers)
        self._write_register(REG_DIRECT_SELECT, 0x00)

    @property
    def name(self) -> str:
        if "E6K5" in self._device_name:
            return f"KLEVV DDR5 RGB (0x{self._addr:02X})"
        return f"ENE DDR5 (0x{self._addr:02X}) {self._device_name}"

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

    def _read_register(self, reg: int) -> int | None:
        try:
            self._bus.write_word_data(self._addr, 0x00, _swap16(reg))
            return self._bus.read_byte_data(self._addr, 0x81)
        except Exception:
            return None

    def _write_register(self, reg: int, value: int) -> None:
        self._bus.write_word_data(self._addr, 0x00, _swap16(reg))
        self._bus.write_byte_data(self._addr, 0x01, value)

    def _write_led_color(self, led_index: int, r: int, g: int, b: int) -> None:
        """Write color to a single LED's effect color registers (R, B, G order)."""
        base = REG_V2_EFFECT_COLOR + led_index * 3
        self._write_register(base, r)
        self._write_register(base + 1, b)
        self._write_register(base + 2, g)

    def set_color(self, r: int, g: int, b: int, zone: int | None = None) -> None:
        r, g, b = self._correct_color(r, g, b)
        if zone is None or zone == 0:
            # All LEDs
            for i in range(self._num_leds):
                self._write_led_color(i, r, g, b)
        else:
            # Individual LED (zone 1 = LED 0, zone 2 = LED 1, etc.)
            led_index = zone - 1
            if 0 <= led_index < self._num_leds:
                self._write_led_color(led_index, r, g, b)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        ene_mode = ENE_DDR5_MODE_MAP.get(mode, 0x01)
        ene_speed = ENE_DDR5_SPEED_MAP.get(speed, 0x03)

        self._write_register(REG_DIRECT_SELECT, 0x00)  # Effect mode
        self._write_register(REG_MODE, ene_mode)
        self._write_register(REG_SPEED, ene_speed)
        self._write_register(REG_DIRECTION, self._direction)

    def apply(self) -> None:
        self._write_register(REG_APPLY, APPLY_VAL)

    @property
    def supports_hardware_save(self) -> bool:
        return True

    def save_to_hardware(self) -> None:
        self._write_register(REG_APPLY, SAVE_VAL)
        log.info("Saved settings to NV flash on %s", self.name)

    def close(self) -> None:
        pass  # SMBus doesn't need per-device close


def _read_ene_name(bus: SMBusInterface, addr: int) -> str | None:
    """Try reading the ENE device name string at register 0x1000."""
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
                return None  # Non-ASCII = not an ENE device
    except Exception:
        return None
    return "".join(chars) if len(chars) >= 4 else None


def detect_ene_ddr5(bus: SMBusInterface) -> list[ENEDDR5Controller]:
    """Detect ENE DDR5 DRAM RGB controllers on the SMBus."""
    controllers = []

    for addr in DDR5_ADDRS:
        try:
            name = _read_ene_name(bus, addr)
            if name is None:
                continue

            if name.startswith("AUD"):
                # Read current direction so we know the "native" direction
                # for even slots (0,2,4,6) and force all to direction 0x00
                slot_index = addr - 0x70
                # Odd slots have reversed LED order — flip direction to match
                direction = 0x01 if (slot_index % 2 == 1) else 0x00
                log.info("Found ENE DDR5 controller at 0x%02X: %s (slot %d, dir=%d)",
                         addr, name, slot_index, direction)
                controllers.append(ENEDDR5Controller(bus, addr, name, direction))
            else:
                log.debug("Unknown device at 0x%02X: %s", addr, name)

        except (IOError, OSError, TimeoutError):
            continue

    return controllers
