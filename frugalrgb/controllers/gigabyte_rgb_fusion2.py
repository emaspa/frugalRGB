"""Gigabyte RGB Fusion 2.0 USB HID controller (ITE IT5711/IT8297 64-byte protocol)."""

import logging
import struct

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

# ITE Tech VID (chip manufacturer used by Gigabyte)
GIGABYTE_VID = 0x048D
REPORT_ID = 0xCC
PACKET_SIZE = 64

# Known ITE chip PIDs
PID_IT8297 = 0x8297
PID_IT8950 = 0x8950
PID_IT5702 = 0x5702
PID_IT5711 = 0x5711

KNOWN_PIDS = {PID_IT8297, PID_IT8950, PID_IT5702, PID_IT5711}

# Effect type constants
EFFECT_NONE = 0
EFFECT_STATIC = 1
EFFECT_PULSE = 2       # Breathing
EFFECT_BLINKING = 3    # Strobe
EFFECT_COLORCYCLE = 4

GB_MODE_MAP = {
    RGBMode.OFF: EFFECT_NONE,
    RGBMode.STATIC: EFFECT_STATIC,
    RGBMode.BREATHING: EFFECT_PULSE,
    RGBMode.STROBE: EFFECT_BLINKING,
    RGBMode.COLOR_CYCLE: EFFECT_COLORCYCLE,
    RGBMode.RAINBOW: EFFECT_COLORCYCLE,  # Color cycle with random color flag
}

HARDWARE_ANIMATED_MODES = {
    RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
}

# LED index -> header register byte
# Indices 0-7 map to 0x20-0x27, indices 8-10 map to 0x90-0x92 (IT5711 only)
_LED_HEADERS = {i: 0x20 + i for i in range(8)}
_LED_HEADERS.update({8: 0x90, 9: 0x91, 10: 0x92})

# Zone layouts: list of (led_index, zone_name) tuples
# X870E Aorus Master / Pro / ICE layout (IT5711)
LAYOUT_X870E = [
    (4, "12V RGB Header"),
    (5, "D_LED1"),
    (6, "D_LED2"),
    (7, "D_LED3"),
    (9, "Logo"),
    (10, "Accent"),
]

# Generic IT8297 layout (older boards: Z390, X570, B550, etc.)
LAYOUT_IT8297 = [
    (0, "Back I/O"),
    (1, "CPU Header"),
    (2, "PCIe"),
    (3, "LED C1/C2"),
    (4, "12V RGB Header"),
    (5, "D_LED1"),
    (6, "D_LED2"),
]

# Chip PID -> default layout
CHIP_LAYOUTS = {
    PID_IT5711: LAYOUT_X870E,
    PID_IT5702: LAYOUT_X870E,
    PID_IT8950: LAYOUT_IT8297,
    PID_IT8297: LAYOUT_IT8297,
}


def _breathing_period(speed: int) -> int:
    """Speed (0-9) to breathing period in ms."""
    if speed <= 6:
        return 400 + speed * 100
    return 1000 + (speed - 6) * 200


def _blinking_period(speed: int) -> int:
    """Speed (0-9) to blinking period in ms."""
    return speed * 200 + 700


def _colorcycle_period(speed: int) -> int:
    """Speed (0-9) to color cycle period in ms."""
    period = speed * 100 + 300
    if speed > 8:
        period += 1300 * (speed - 8)
    return period


class GigabyteRGBFusion2Controller(RGBController):
    """Controller for Gigabyte RGB Fusion 2.0 via USB HID (ITE IT5711/IT8297)."""

    def __init__(self, dev, device_path: str, pid: int, product_string: str):
        self._dev = dev
        self._path = device_path
        self._pid = pid
        self._product_string = product_string.strip()
        self._current_mode = RGBMode.STATIC
        self._current_speed = 3
        self._color_correction = (1.0, 1.0, 1.0)
        self._zones: list[RGBZone] = []
        self._zone_leds: list[int | None] = []  # LED index per zone (None = all)
        self._device_name = ""
        self._fw_version = ""
        self._chip_id = 0
        self._is_5711 = pid in (PID_IT5711, PID_IT5702)
        self._accumulated_mask = 0

        self._init_device()

    # -- Low-level HID --

    def _send_packet(self, pkt: bytearray) -> None:
        self._dev.send_feature_report(list(pkt))

    def _recv_packet(self) -> list[int]:
        return list(self._dev.get_feature_report(REPORT_ID, PACKET_SIZE))

    def _send_cmd(self, a: int, b: int = 0, c: int = 0) -> None:
        pkt = bytearray(PACKET_SIZE)
        pkt[0] = REPORT_ID
        pkt[1] = a
        pkt[2] = b
        pkt[3] = c
        self._send_packet(pkt)

    # -- Init --

    def _init_device(self) -> None:
        # Read device info (cmd 0x60)
        self._send_cmd(0x60)
        info = self._recv_packet()

        if len(info) >= 40:
            name_bytes = bytes(info[12:40])
            self._device_name = name_bytes.split(b"\x00")[0].decode(
                "ascii", errors="replace"
            )
        if len(info) >= 8:
            self._fw_version = ".".join(str(b) for b in info[4:8])
        if len(info) >= 60:
            self._chip_id = struct.unpack_from("<I", bytes(info[56:60]))[0]

        log.info(
            "Gigabyte RGB: product=%s fw=%s chip=0x%08X",
            self._device_name, self._fw_version, self._chip_id,
        )

        # Extended calibration for IT5711
        if self._is_5711:
            self._send_cmd(0x61)
            self._recv_packet()

        # Disable Lamp Array mode — required on Windows 11 to prevent
        # Dynamic Lighting from overriding our color commands
        self._send_cmd(0x48, 0x00)

        # Reset all effect registers
        for reg in range(0x20, 0x28):
            self._send_cmd(reg, 0x00, 0x00)
        if self._is_5711:
            for reg in range(0x90, 0x93):
                self._send_cmd(reg, 0x00, 0x00)

        # Fast apply after reset
        self._send_cmd(0x28, 0xFF, 0x07 if self._is_5711 else 0x00)

        # Disable beat/audio mode
        self._send_cmd(0x31, 0x00)

        # Enable built-in effects on all ARGB headers (0x00 = none disabled)
        self._send_cmd(0x32, 0x00)

        self._setup_zones()

    def _setup_zones(self) -> None:
        layout = CHIP_LAYOUTS.get(self._pid, LAYOUT_IT8297)

        self._zones = [RGBZone(0, "All Zones")]
        self._zone_leds = [None]

        for led_idx, zone_name in layout:
            zid = len(self._zones)
            self._zones.append(RGBZone(zid, zone_name))
            self._zone_leds.append(led_idx)

        log.info("Gigabyte RGB: %d zone(s)", len(self._zones))

    # -- Effect packet --

    def _build_effect_pkt(self, led_index: int, r: int, g: int, b: int) -> bytearray:
        """Build a PktEffect. led_index=-1 means all zones."""
        pkt = bytearray(PACKET_SIZE)
        pkt[0] = REPORT_ID

        if led_index < 0:
            # All zones at once
            pkt[1] = 0x20
            mask = 0x07FF if self._is_5711 else 0xFF
        else:
            # Single zone
            pkt[1] = _LED_HEADERS.get(led_index, 0x20)
            mask = 1 << led_index
        struct.pack_into("<I", pkt, 2, mask)

        # Effect type
        effect = GB_MODE_MAP.get(self._current_mode, EFFECT_STATIC)
        pkt[11] = effect

        # Brightness (cap at 100 for breathing due to hardware quirk)
        pkt[12] = 100 if effect == EFFECT_PULSE else 0xFF

        # Color0 as uint32 LE: byte 14 = B, byte 15 = G, byte 16 = R
        struct.pack_into("<I", pkt, 14, b | (g << 8) | (r << 16))

        # Timing periods
        hw_speed = min(9, self._current_speed * 2)  # map 0-5 → 0-10, capped at 9
        if self._current_mode == RGBMode.BREATHING:
            struct.pack_into("<H", pkt, 22, _breathing_period(hw_speed))
        elif self._current_mode == RGBMode.STROBE:
            struct.pack_into("<H", pkt, 26, _blinking_period(hw_speed))
        elif self._current_mode in (RGBMode.COLOR_CYCLE, RGBMode.RAINBOW):
            struct.pack_into("<H", pkt, 22, _colorcycle_period(hw_speed))

        # Random color flag for rainbow
        if self._current_mode == RGBMode.RAINBOW:
            pkt[30] = 7

        self._accumulated_mask |= mask
        return pkt

    # -- RGBController interface --

    @property
    def name(self) -> str:
        if self._device_name:
            return f"Gigabyte RGB ({self._device_name})"
        return "Gigabyte RGB Fusion 2.0"

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
            # Single packet addressing all zones at once
            self._send_packet(self._build_effect_pkt(-1, r, g, b))
        else:
            if 0 < zone < len(self._zone_leds):
                led_idx = self._zone_leds[zone]
                if led_idx is not None:
                    self._send_packet(self._build_effect_pkt(led_idx, r, g, b))

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        self._current_speed = speed

    def apply(self) -> None:
        pkt = bytearray(PACKET_SIZE)
        pkt[0] = REPORT_ID
        pkt[1] = 0x28
        struct.pack_into("<I", pkt, 2, self._accumulated_mask)
        self._send_packet(pkt)
        self._accumulated_mask = 0

    @property
    def supports_hardware_save(self) -> bool:
        return True

    def save_to_hardware(self) -> None:
        self._send_cmd(0x47, 0x01)
        log.info("Saved settings to hardware on %s", self.name)

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None


def detect_gigabyte_rgb_fusion2() -> GigabyteRGBFusion2Controller | None:
    """Detect Gigabyte RGB Fusion 2.0 USB HID controller (ITE IT5711/IT8297)."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping Gigabyte detection")
        return None

    for dev_info in hid.enumerate(GIGABYTE_VID):
        pid = dev_info.get("product_id", 0)
        usage_page = dev_info.get("usage_page", 0)
        usage = dev_info.get("usage", 0)
        product = dev_info.get("product_string", "") or ""

        # Match by HID usage (0xFF89:0xCC) or known PID. Don't filter on
        # interface number: Windows exposes the RGB collection on interface 0,
        # but hidapi's libusb backend reports no usage info and the feature
        # reports may only answer on another interface (IT5711: interface 1).
        # The 0x60 info-read below verifies each candidate.
        if usage_page == 0xFF89 and usage == 0xCC:
            pass
        elif pid in KNOWN_PIDS:
            pass
        else:
            continue

        path = dev_info.get("path", b"")
        if isinstance(path, bytes):
            path_str = path.decode("utf-8", errors="replace")
        else:
            path_str = str(path)

        try:
            dev = hid.device()
            dev.open_path(path)

            # Verify by reading device info
            pkt = bytearray(PACKET_SIZE)
            pkt[0] = REPORT_ID
            pkt[1] = 0x60
            dev.send_feature_report(list(pkt))
            info = list(dev.get_feature_report(REPORT_ID, PACKET_SIZE))

            # Check for a valid product string at bytes 12-39
            if len(info) >= 40:
                name = bytes(info[12:40]).split(b"\x00")[0].decode(
                    "ascii", errors="replace"
                )
                if not name:
                    log.debug("Gigabyte PID 0x%04X: empty product string, skipping",
                              pid)
                    dev.close()
                    continue
            else:
                log.debug("Gigabyte PID 0x%04X: info report too short, skipping", pid)
                dev.close()
                continue

            log.info("Found Gigabyte RGB Fusion 2.0 at %s (PID=0x%04X, %s)",
                     path_str, pid, product.strip())
            return GigabyteRGBFusion2Controller(dev, path_str, pid, product)

        except Exception as e:
            # Expected for non-RGB interfaces of a known PID — keep probing.
            log.debug("Gigabyte RGB candidate %s (PID=0x%04X) rejected: %s",
                      path_str, pid, e)
            continue

    return None
