"""MSI Mystic Light USB HID controller (185-byte protocol)."""

import logging

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

MSI_VID = 0x1462
REPORT_ID = 0x52
PACKET_SIZE = 185

# Mode values
MSI_MODE_DISABLE = 0x00
MSI_MODE_STATIC = 0x01
MSI_MODE_BREATHING = 0x02
MSI_MODE_FLASHING = 0x03
MSI_MODE_DOUBLE_FLASHING = 0x04
MSI_MODE_LIGHTNING = 0x05
MSI_MODE_RAINBOW = 0x25  # Rainbow wave
MSI_MODE_COLOR_CYCLE = 0x04  # Color cycle (uses rainbow flag)

MSI_MODE_MAP = {
    RGBMode.OFF: MSI_MODE_DISABLE,
    RGBMode.STATIC: MSI_MODE_STATIC,
    RGBMode.BREATHING: MSI_MODE_BREATHING,
    RGBMode.STROBE: MSI_MODE_FLASHING,
    RGBMode.COLOR_CYCLE: 0x15,  # Color shift
    RGBMode.RAINBOW: 0x19,      # Rainbow (mode 25)
}

# Speed: 0 = low, 1 = medium, 2 = high
MSI_SPEED_MAP = {0: 2, 1: 2, 2: 1, 3: 1, 4: 0, 5: 0}

# Brightness 0-10 (10 = 100%)
DEFAULT_BRIGHTNESS = 10

# Zone offsets within the 185-byte packet
# Each ZoneData is 10 bytes, RainbowZoneData is 11 bytes, CorsairZoneData is 11 bytes
ZONE_DEFS = {
    "J_RGB_1":       {"offset":  1, "size": 10, "type": "zone"},
    "J_PIPE_1":      {"offset": 11, "size": 10, "type": "zone"},
    "J_PIPE_2":      {"offset": 21, "size": 10, "type": "zone"},
    "J_RAINBOW_1":   {"offset": 31, "size": 11, "type": "rainbow"},
    "J_RAINBOW_2":   {"offset": 42, "size": 11, "type": "rainbow"},
    "J_CORSAIR":     {"offset": 53, "size": 11, "type": "corsair"},
    "J_CORSAIR_OUT": {"offset": 64, "size": 10, "type": "zone"},
    "ONBOARD":       {"offset": 74, "size": 10, "type": "zone"},
    "ONBOARD_1":     {"offset": 84, "size": 10, "type": "zone"},
    "ONBOARD_2":     {"offset": 94, "size": 10, "type": "zone"},
    "ONBOARD_3":     {"offset": 104, "size": 10, "type": "zone"},
    "ONBOARD_4":     {"offset": 114, "size": 10, "type": "zone"},
    "ONBOARD_5":     {"offset": 124, "size": 10, "type": "zone"},
    "ONBOARD_6":     {"offset": 134, "size": 10, "type": "zone"},
    "ONBOARD_7":     {"offset": 144, "size": 10, "type": "zone"},
    "ONBOARD_8":     {"offset": 154, "size": 10, "type": "zone"},
    "ONBOARD_9":     {"offset": 164, "size": 10, "type": "zone"},
    "J_RGB_2":       {"offset": 174, "size": 10, "type": "zone"},
}

# Hardware-animated modes (no software loop needed)
HARDWARE_ANIMATED_MODES = {
    RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
}

# Board database: PID -> board info
# Boards with PID >= 0x7D03 use "mixed mode" zone remapping
MSI_BOARD_DB: dict[int, dict] = {
    # Add known boards here. Unknown PIDs get a generic config.
    0x7E03: {"name": "MSI MPG Z790I EDGE WIFI", "onboard_leds": 6, "jrainbow1": True},
}


def _pack_speed_brightness(brightness: int = DEFAULT_BRIGHTNESS,
                           speed: int = 1) -> int:
    return ((brightness & 0x1F) << 2) | (speed & 0x03)


class MSIMysticLightController(RGBController):
    """Controller for MSI Mystic Light USB HID (185-byte protocol)."""

    def __init__(self, dev, device_path: str, pid: int, product_string: str):
        self._dev = dev
        self._path = device_path
        self._pid = pid
        self._product_string = product_string.strip()
        self._current_mode = RGBMode.STATIC
        self._current_speed = 1  # MSI speed: 0=low, 1=medium, 2=high
        self._color_correction = (1.0, 1.0, 1.0)
        self._state: list[int] | None = None
        self._zones: list[RGBZone] = []
        self._zone_keys: list[str] = []  # zone key per RGBZone index

        board = MSI_BOARD_DB.get(pid, {})
        self._board_name = board.get("name", f"MSI Mystic Light (0x{pid:04X})")
        self._onboard_leds = board.get("onboard_leds", 0)

        self._read_state()
        self._detect_zones()

    def _read_state(self) -> None:
        """Read the full 185-byte state from the device."""
        try:
            self._state = list(self._dev.get_feature_report(REPORT_ID, PACKET_SIZE))
            if len(self._state) < PACKET_SIZE:
                log.warning("MSI: got %d bytes (expected %d), padding",
                            len(self._state), PACKET_SIZE)
                self._state += [0] * (PACKET_SIZE - len(self._state))
            log.debug("MSI: read state (%d bytes)", len(self._state))
        except Exception as e:
            log.error("MSI: failed to read state: %s", e)
            self._state = [0] * PACKET_SIZE
            self._state[0] = REPORT_ID

    def _detect_zones(self) -> None:
        """Determine available zones from the state packet."""
        self._zones = []
        self._zone_keys = []

        # Onboard LEDs zone (always present if we got a valid state)
        if self._state and self._state[0] == REPORT_ID:
            self._zones.append(RGBZone(0, "All Zones"))
            self._zone_keys.append("ALL")

            # Check individual zones by looking at whether their effect byte
            # is something other than 0x00 (disabled), or just expose known ones
            if self._onboard_leds > 0:
                self._zones.append(RGBZone(1, "Onboard LEDs"))
                self._zone_keys.append("ONBOARD")

            # Check JRAINBOW1 — if the board has it
            board = MSI_BOARD_DB.get(self._pid, {})
            if board.get("jrainbow1"):
                self._zones.append(RGBZone(2, "JRAINBOW1"))
                self._zone_keys.append("J_RAINBOW_1")

        log.info("MSI Mystic Light: %d zone(s) detected", len(self._zones))

    def _set_zone_data(self, zone_key: str, mode: int, r: int, g: int, b: int,
                       speed: int = 1, brightness: int = DEFAULT_BRIGHTNESS,
                       rainbow: bool = False) -> None:
        """Write zone data into the state buffer."""
        zdef = ZONE_DEFS.get(zone_key)
        if zdef is None or self._state is None:
            return

        offset = zdef["offset"]
        flags = _pack_speed_brightness(brightness, speed)
        color_flags = 0x00 if rainbow else 0x80

        if zone_key == "ONBOARD":
            color_flags |= 0x01  # SYNC_SETTING_ONBOARD

        self._state[offset + 0] = mode
        self._state[offset + 1] = r
        self._state[offset + 2] = g
        self._state[offset + 3] = b
        self._state[offset + 4] = flags
        self._state[offset + 5] = r  # color2
        self._state[offset + 6] = g
        self._state[offset + 7] = b
        self._state[offset + 8] = color_flags
        self._state[offset + 9] = 0x00

        # RainbowZoneData has an extra byte for LED count
        if zdef["type"] == "rainbow" and zdef["size"] >= 11:
            self._state[offset + 10] = 100  # default cycle/led count

    def _get_all_zone_keys(self) -> list[str]:
        """Return all zone keys that should be set when applying to 'All Zones'."""
        keys = []
        # Master onboard + individual onboard LEDs
        keys.append("ONBOARD")
        for i in range(self._onboard_leds):
            key = f"ONBOARD_{i + 1}"
            if key in ZONE_DEFS:
                keys.append(key)
        # Headers
        for key in ("J_RGB_1", "J_RGB_2", "J_PIPE_1", "J_PIPE_2",
                     "J_RAINBOW_1", "J_RAINBOW_2"):
            if key in ZONE_DEFS:
                keys.append(key)
        return keys

    @property
    def name(self) -> str:
        return self._board_name

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
        mode_val = MSI_MODE_MAP.get(self._current_mode, MSI_MODE_STATIC)
        rainbow = self._current_mode == RGBMode.RAINBOW
        speed = self._current_speed

        if zone is None or zone == 0:
            # All zones
            for key in self._get_all_zone_keys():
                self._set_zone_data(key, mode_val, r, g, b,
                                    speed=speed, rainbow=rainbow)
        else:
            # Specific zone
            if 0 <= zone < len(self._zone_keys):
                key = self._zone_keys[zone]
                if key == "ALL":
                    for k in self._get_all_zone_keys():
                        self._set_zone_data(k, mode_val, r, g, b,
                                            speed=speed, rainbow=rainbow)
                elif key == "ONBOARD":
                    self._set_zone_data("ONBOARD", mode_val, r, g, b,
                                        speed=speed, rainbow=rainbow)
                    for i in range(self._onboard_leds):
                        k = f"ONBOARD_{i + 1}"
                        if k in ZONE_DEFS:
                            self._set_zone_data(k, mode_val, r, g, b,
                                                speed=speed, rainbow=rainbow)
                else:
                    self._set_zone_data(key, mode_val, r, g, b,
                                        speed=speed, rainbow=rainbow)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        self._current_speed = MSI_SPEED_MAP.get(speed, 1)

    def apply(self, save: bool = False) -> None:
        if self._state is None:
            return
        self._state[184] = 1 if save else 0
        try:
            self._dev.send_feature_report(self._state)
            log.debug("MSI: sent %d-byte feature report (save=%s)",
                      len(self._state), save)
        except Exception as e:
            log.error("MSI: failed to send feature report: %s", e)

    @property
    def supports_hardware_save(self) -> bool:
        return True

    def save_to_hardware(self) -> None:
        self.apply(save=True)
        log.info("Saved settings to hardware on %s", self.name)

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None


def detect_msi_mystic_light() -> MSIMysticLightController | None:
    """Detect MSI Mystic Light USB HID controller (185-byte protocol)."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping MSI detection")
        return None

    for dev_info in hid.enumerate(MSI_VID):
        product = dev_info.get("product_string", "") or ""
        pid = dev_info.get("product_id", 0)
        usage_page = dev_info.get("usage_page", 0)
        usage = dev_info.get("usage", 0)

        # 185-byte protocol boards use usage_page=0x0001, usage=0x00
        if usage_page != 0x0001 or usage != 0x00:
            continue

        # Match by product string or known PID
        is_mystic = "MYSTIC LIGHT" in product.upper()
        is_known_pid = pid in MSI_BOARD_DB

        if not is_mystic and not is_known_pid:
            continue

        path = dev_info.get("path", b"")
        if isinstance(path, bytes):
            path_str = path.decode("utf-8", errors="replace")
        else:
            path_str = str(path)

        try:
            dev = hid.device()
            dev.open_path(path)

            # Verify it's a 185-byte protocol by reading the feature report
            state = dev.get_feature_report(REPORT_ID, PACKET_SIZE)
            if len(state) < 100:
                log.debug("MSI PID 0x%04X: report too short (%d bytes), skipping",
                          pid, len(state))
                dev.close()
                continue

            log.info("Found MSI Mystic Light at %s (PID=0x%04X, %s, %d bytes)",
                     path_str, pid, product.strip(), len(state))
            return MSIMysticLightController(dev, path_str, pid, product)

        except Exception as e:
            log.warning("Failed to open MSI Mystic Light (PID=0x%04X): %s", pid, e)
            continue

    return None
