"""ASUS Aura USB HID motherboard RGB controller (VID 0x0B05, 65-byte protocol)."""

import logging

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

ASUS_AURA_VID = 0x0B05

# Confirmed working PIDs
KNOWN_PIDS = {0x19AF}
# Reported but not verified
EXPERIMENTAL_PIDS = {0x1939, 0x18F3}

REPORT_SIZE = 65  # 1 byte report ID + 64 bytes data

# Command prefix
CMD = 0xEC

# Function codes (second byte of each packet)
FUNC_FW = 0x82       # Request firmware version
FUNC_CONFIG = 0xB0   # Request config table
FUNC_EFFECT = 0x35   # Effect configuration / sequence control
FUNC_COLOR = 0x36    # Color data
FUNC_END = 0x3F      # End sequence marker
FUNC_DIRECT = 0x40   # Direct mode control

# Channel definitions: (effect_id, direct_id, type_code, rgb_offset)
# type_code: 0 = 12V RGB, 1 = 5V ARGB
# rgb_offset: position of RGB triplet in color packet (0-based, multiplied by 3)
CHANNELS = {
    "led1": (0x01, 0x00, 0x00, 0),   # Onboard RGB (12V header)
    "led2": (0x02, 0x00, 0x01, 0),   # ARGB Header 1
    "led3": (0x04, 0x01, 0x01, 1),   # ARGB Header 2
    "led4": (0x08, 0x02, 0x01, 2),   # ARGB Header 3
}

CHANNEL_KEYS = ["led1", "led2", "led3", "led4"]

ZONE_NAMES = ["Onboard RGB", "ARGB Header 1", "ARGB Header 2", "ARGB Header 3"]

# Mode mapping (frugalrgb modes -> ASUS Aura mode values)
AURA_MODE_MAP = {
    RGBMode.OFF: 0x00,
    RGBMode.STATIC: 0x01,
    RGBMode.BREATHING: 0x02,
    RGBMode.STROBE: 0x03,
    RGBMode.COLOR_CYCLE: 0x04,
    RGBMode.RAINBOW: 0x05,
}

# Modes that the hardware animates on its own
HARDWARE_ANIMATED_MODES = {
    RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
}

# Modes that require a color argument
COLOR_MODES = {0x01, 0x02, 0x03}  # static, breathing, flashing


def _pad(data: list[int]) -> list[int]:
    """Pad a command to REPORT_SIZE bytes."""
    return (data + [0x00] * REPORT_SIZE)[:REPORT_SIZE]


class AsusAuraUSBController(RGBController):
    """Controller for ASUS Aura motherboard RGB via USB HID (VID 0x0B05)."""

    def __init__(self, dev, device_path: str, pid: int, product_string: str):
        self._dev = dev
        self._path = device_path
        self._pid = pid
        self._product_string = product_string.strip()
        self._current_mode = RGBMode.STATIC
        self._current_speed = 3
        self._color_correction = (1.0, 1.0, 1.0)
        self._firmware = ""
        self._pending_colors: dict[str, tuple[int, int, int]] = {}

        self._init_device()
        self._setup_zones()

    # -- Low-level HID --

    def _send(self, data: list[int]) -> None:
        """Send a 65-byte feature report (report ID 0x00 + 64 data bytes).
        The ASUS Aura protocol is fire-and-forget — no response is expected."""
        self._dev.send_feature_report(_pad([0x00] + data))

    # -- Init --

    def _init_device(self) -> None:
        # The Aura protocol is fire-and-forget: no read responses.
        self._firmware = "unknown"

        # Exit any active direct/effect mode (5 iterations as per protocol)
        for _ in range(5):
            self._send([CMD, FUNC_EFFECT, 0x00, 0x00, 0x00, 0xFF])
        for _ in range(5):
            self._send([CMD, FUNC_END, 0x55, 0x00, 0x00])

    def _setup_zones(self) -> None:
        self._zones = [RGBZone(0, "All Zones")]
        for i, name in enumerate(ZONE_NAMES):
            self._zones.append(RGBZone(i + 1, name))

    # -- RGBController interface --

    @property
    def name(self) -> str:
        if self._product_string:
            return f"ASUS Aura USB ({self._product_string})"
        return f"ASUS Aura USB (0x{self._pid:04X})"

    @property
    def zones(self) -> list[RGBZone]:
        return self._zones

    @property
    def supported_modes(self) -> list[RGBMode]:
        return [RGBMode.OFF, RGBMode.STATIC, RGBMode.BREATHING,
                RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW]

    @property
    def has_hardware_mode(self) -> bool:
        return self._current_mode in HARDWARE_ANIMATED_MODES

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
            for key in CHANNEL_KEYS:
                self._pending_colors[key] = (r, g, b)
        else:
            idx = zone - 1
            if 0 <= idx < len(CHANNEL_KEYS):
                self._pending_colors[CHANNEL_KEYS[idx]] = (r, g, b)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        self._current_speed = speed

    def apply(self) -> None:
        if not self._pending_colors:
            return

        mode_val = AURA_MODE_MAP.get(self._current_mode, 0x01)
        needs_color = mode_val in COLOR_MODES

        # Reset before applying new effect
        for _ in range(5):
            self._send([CMD, FUNC_EFFECT, 0x00, 0x00, 0x00, 0xFF])
        for _ in range(5):
            self._send([CMD, FUNC_END, 0x55, 0x00, 0x00])

        # Send effect + color for each pending channel
        for key, (r, g, b) in self._pending_colors.items():
            effect_id, _direct_id, type_code, rgb_offset = CHANNELS[key]

            # Effect command: set mode for this channel
            self._send([CMD, FUNC_EFFECT, type_code, 0x00, 0x00, mode_val])

            # Color command: set RGB for this channel
            color_data = [CMD, FUNC_COLOR, 0x00, effect_id, 0x00]
            color_data += [0x00] * (3 * rgb_offset)
            if needs_color:
                color_data += [r, g, b]
            self._send(color_data)

        # End sequence
        self._send([CMD, FUNC_END, 0x55, 0x00, 0x00])
        # Commit
        self._send([CMD, FUNC_EFFECT, 0x00, 0x00, 0x01, 0x05])
        self._send([CMD, FUNC_END, 0x55, 0x00, 0x00])

        self._pending_colors.clear()

    def close(self) -> None:
        if self._dev:
            try:
                self._send([CMD, FUNC_DIRECT, 0x00])
            except Exception:
                pass
            self._dev.close()
            self._dev = None


def detect_asus_aura_usb() -> "AsusAuraUSBController | None":
    """Detect ASUS Aura USB RGB controller on the motherboard."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping ASUS Aura USB detection")
        return None

    all_pids = KNOWN_PIDS | EXPERIMENTAL_PIDS

    for dev_info in hid.enumerate(ASUS_AURA_VID):
        pid = dev_info.get("product_id", 0)
        if pid not in all_pids:
            continue

        # Don't filter by interface_number — hidapi returns -1 on Windows
        # for some devices. Verification via firmware query handles false positives.
        product = dev_info.get("product_string", "") or ""
        path = dev_info.get("path", b"")
        if isinstance(path, bytes):
            path_str = path.decode("utf-8", errors="replace")
        else:
            path_str = str(path)

        if pid in EXPERIMENTAL_PIDS:
            log.warning("ASUS Aura PID 0x%04X is experimental — please report issues",
                        pid)

        try:
            dev = hid.device()
            dev.open_path(path)

            # Verify by sending a feature report — protocol is fire-and-forget,
            # no response expected. If send_feature_report raises, it's not our device.
            dev.send_feature_report(_pad([0x00, CMD, FUNC_FW]))

            log.info("Found ASUS Aura USB at %s (PID=0x%04X, %s)",
                     path_str, pid, product.strip())
            return AsusAuraUSBController(dev, path_str, pid, product)

        except Exception as e:
            log.warning("Failed to open ASUS Aura USB (PID=0x%04X): %s — "
                        "is Armoury Crate / ASUS LightingService running?", pid, e)
            continue

    return None
