import logging
import sys

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

ASROCK_VID = 0x26CE
ASROCK_PID = 0x01A2
REPORT_SIZE = 65  # 1 byte report ID + 64 bytes data

# USB HID command bytes (usb_buf[0x01])
CMD_SET_ZONE = 0x10
CMD_READ_ZONE = 0x11
CMD_COMMIT = 0x12
CMD_READ_HEADER = 0x14
CMD_WRITE_HEADER = 0x15
CMD_INIT = 0xA4

# Config table IDs
CFG_ZONE_AVAIL = 0x01
CFG_LED_COUNT = 0x02
CFG_RGSWAP = 0x03

# Zone unavailable marker
ZONE_UNAVAILABLE = 0x1E

# Mode values
ASROCK_USB_MODE_MAP = {
    RGBMode.OFF: 0x00,
    RGBMode.STATIC: 0x01,
    RGBMode.BREATHING: 0x02,
    RGBMode.STROBE: 0x03,
    RGBMode.COLOR_CYCLE: 0x04,
    RGBMode.RAINBOW: 0x0E,  # 0x0E = Rainbow (0x05 is Random in OpenRGB)
}

# Speed: 0x00 = fast, 0xFF = slow, 0xE0 = default
SPEED_DEFAULT = 0xE0

# Zone type IDs (matching OpenRGB exactly)
ZONE_RGB_HEADER_1 = 0x00
ZONE_RGB_HEADER_2 = 0x01
ZONE_ARGB_HEADER_1 = 0x02
ZONE_ARGB_HEADER_2 = 0x03
ZONE_PCH = 0x04
ZONE_IO_COVER = 0x05
ZONE_PCB = 0x06
ZONE_AUDIO = 0x07

ZONE_NAMES = [
    "RGB Header 1", "RGB Header 2",
    "ARGB Header 1", "ARGB Header 2",
    "PCH", "IO Cover", "PCB", "Audio/ARGB3",
]

# Hardware modes that the device animates on its own (no software loop needed)
HARDWARE_ANIMATED_MODES = {
    RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
}

# Per-channel color correction for LED brightness imbalance (R, G, B multipliers).
# Start at 1.0 — adjust if colors look off on your hardware.
DEFAULT_COLOR_CORRECTION = (1.0, 1.0, 1.0)


class ASRockPolychromeUSBController(RGBController):
    """Controller for ASRock Polychrome RGB via USB HID (VID_26CE)."""

    def __init__(self, dev, device_path: str):
        self._dev = dev
        self._path = device_path
        self._current_mode = RGBMode.STATIC
        self._current_speed = SPEED_DEFAULT
        self._zones: list[RGBZone] = []
        self._color_correction = DEFAULT_COLOR_CORRECTION
        self._read_config_tables()

    def _hid_write_read(self, buf: list[int]) -> list[int]:
        """Send a 65-byte report and read the 64-byte response."""
        packet = buf + [0x00] * (REPORT_SIZE - len(buf))
        self._dev.write(packet)
        response = self._dev.read(64, timeout_ms=1000)
        return response if response else []

    def _read_config_tables(self) -> None:
        """Read config tables and initialize zones (matches OpenRGB protocol)."""
        # Config table 0x02: LED counts per zone (bytes [0x04..0x0B])
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_READ_HEADER
        buf[0x03] = CFG_LED_COUNT
        resp = self._hid_write_read(buf)
        led_counts = resp[0x04:0x0C] if resp else [ZONE_UNAVAILABLE] * 8

        # Config table 0x03: RGSwap bitmask (byte [0x04])
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_READ_HEADER
        buf[0x03] = CFG_RGSWAP
        resp = self._hid_write_read(buf)
        rgswap_byte = resp[0x04] if resp else 0x00

        # Config table 0x01: zone availability bitmask (byte [0x04])
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_READ_HEADER
        buf[0x03] = CFG_ZONE_AVAIL
        resp = self._hid_write_read(buf)
        zone_avail = resp[0x04] if resp else 0xFF

        # Determine available zones
        self._zones = []
        for i in range(8):
            available = led_counts[i] != ZONE_UNAVAILABLE and ((zone_avail >> i) & 1)
            if available:
                self._zones.append(RGBZone(i, ZONE_NAMES[i]))
                log.debug("Zone %d (%s): %d LEDs", i, ZONE_NAMES[i], led_counts[i])

        log.info("ASRock Polychrome: %d zone(s) available", len(self._zones))

        # Disable hardware RGSwap — we send in the hardware's native G,R,B format.
        # Writing the stored rgswap value back causes a double-swap (hardware swaps
        # AND software swaps), so we disable hardware swapping entirely.
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_WRITE_HEADER
        buf[0x03] = CFG_RGSWAP
        buf[0x04] = 0x00  # All zeros = no hardware swap
        self._hid_write_read(buf)

    @property
    def name(self) -> str:
        return "ASRock Polychrome USB"

    @property
    def zones(self) -> list[RGBZone]:
        return self._zones

    @property
    def supported_modes(self) -> list[RGBMode]:
        return [RGBMode.OFF, RGBMode.STATIC, RGBMode.BREATHING,
                RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW]

    @property
    def color_correction(self) -> tuple[float, float, float]:
        return self._color_correction

    @color_correction.setter
    def color_correction(self, value: tuple[float, float, float]) -> None:
        self._color_correction = value

    @property
    def has_hardware_mode(self) -> bool:
        """True if current mode is animated by the hardware (no software loop needed)."""
        return self._current_mode in HARDWARE_ANIMATED_MODES

    def _correct_color(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        """Apply per-channel color correction."""
        cr, cg, cb = self._color_correction
        return (
            min(255, int(r * cr)),
            min(255, int(g * cg)),
            min(255, int(b * cb)),
        )

    def set_color(self, r: int, g: int, b: int, zone: int | None = None) -> None:
        mode_val = ASROCK_USB_MODE_MAP.get(self._current_mode, 0x01)
        r, g, b = self._correct_color(r, g, b)

        if zone is not None:
            self._write_zone(zone, mode_val, r, g, b, all_zones=False)
        else:
            for z in self._zones:
                self._write_zone(z.zone_id, mode_val, r, g, b, all_zones=False)

    def _write_zone(self, zone_type: int, mode: int, r: int, g: int, b: int,
                    all_zones: bool = False) -> None:
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_SET_ZONE
        buf[0x03] = zone_type
        buf[0x04] = mode

        # Hardware native format: byte[5]=G, byte[6]=R, byte[7]=B
        # (RGSwap is disabled via WriteHeader so no hardware channel swapping)
        buf[0x05] = g
        buf[0x06] = r
        buf[0x07] = b

        buf[0x08] = self._current_speed
        buf[0x09] = 0xFF
        buf[0x10] = 0x01 if all_zones else 0x00

        self._hid_write_read(buf)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        # Map speed 0-5 to 0x00-0xFF range
        self._current_speed = int(speed * 51)  # 0→0, 5→255

    def apply(self) -> None:
        buf = [0x00] * REPORT_SIZE
        buf[0x01] = CMD_COMMIT
        self._hid_write_read(buf)

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None


def detect_asrock_polychrome_usb() -> ASRockPolychromeUSBController | None:
    """Detect ASRock Polychrome USB RGB controller."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping USB HID detection")
        return None

    devices = hid.enumerate(ASROCK_VID, ASROCK_PID)
    if not devices:
        return None

    try:
        dev = hid.device()
        dev.open(ASROCK_VID, ASROCK_PID)
        path = devices[0].get("path", b"").decode("utf-8", errors="replace")
        log.info("Found ASRock Polychrome USB at %s", path)
        return ASRockPolychromeUSBController(dev, path)
    except Exception as e:
        log.warning("Failed to open ASRock Polychrome USB: %s", e)
        return None
