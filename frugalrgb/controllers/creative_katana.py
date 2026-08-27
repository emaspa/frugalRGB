"""Creative Sound BlasterX Katana soundbar RGB via USB HID (VID 0x041E).

Protocol: 64-byte frames [0x5A magic, command, data length, data...] on the
64-byte HID interface (interface 4). Lighting is command 0x3A with
subcommands; state is stored per pattern *type* inside the currently active
device profile, so all writes edit (and persist to) the active profile:

  0x3A 0x10                       -> get active profile number
  0x3A 0x08 <prof> <type>         -> set active pattern type
  0x3A 0x04 0x00 <type> <ngroups> 0x00 <49 x slot>   -> LED->palette-slot map
  0x3A 0x0A 0x00 <type> 0x01 <slot> <entries>        -> set palette entries
  0x3A 0x0A 0x00 <type> 0x03 <period LE16>           -> set speed (60000/BPM)
  0x3A 0x06 <0|1>                 -> lights off/on

Palette entries are 4 bytes: [alpha, blue, green, red]. The bar has 49 LEDs
addressed through 7 groups of 7. Sets are acknowledged with an 0x02 frame
carrying result code 0x00 (success); gets are answered with an echo frame.
Reverse-engineered from the therion23/KatanaHacking notes, a USB capture from
OpenRGB issue #1065, and live probing.
"""

import logging
import time

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

KATANA_VID = 0x041E
KATANA_PID = 0x3247
COMMAND_INTERFACE = 4
PACKET_SIZE = 64

MAGIC = 0x5A
CMD_ERROR = 0x02
CMD_SYSINFO = 0x07
CMD_LIGHTING = 0x3A

# Lighting subcommands
SUB_SET_PATTERN = 0x04
SUB_GET_PATTERN = 0x05
SUB_ONOFF = 0x06
SUB_SET_TYPE = 0x08
SUB_GET_TYPE = 0x09
SUB_SET_PARAM = 0x0A
SUB_GET_PARAM = 0x0B
SUB_CUR_PROFILE = 0x10
SUB_GET_NAME = 0x16

# Parameter keys for SUB_SET_PARAM / SUB_GET_PARAM
PARAM_PALETTE = 0x01
PARAM_SPEED = 0x03
PARAM_DIRECTION = 0x04

# Pattern types (hardware effects)
TYPE_PULSATE = 0x01   # breathing
TYPE_STATIC = 0x03    # solo (1 color) / mood (7 zone colors)
TYPE_WAVE = 0x04
TYPE_CYCLE = 0x05     # moving rainbow
TYPE_AURORA = 0x08

NUM_LEDS = 49
NUM_GROUPS = 7

# Speed 0 (fastest) to 5 (slowest) -> effect period in ms (BPM = 60000/period)
SPEED_PERIODS = [250, 500, 1000, 2500, 4000, 6000]

# ROYGBIV palette in device [alpha, blue, green, red] order (factory rainbow)
RAINBOW_PALETTE = bytes([
    0xFF, 0x00, 0x00, 0xFF,  # red
    0xFF, 0x00, 0x7F, 0xFF,  # orange
    0xFF, 0x00, 0xFF, 0xFF,  # yellow
    0xFF, 0x00, 0xFF, 0x00,  # green
    0xFF, 0xFF, 0x00, 0x00,  # blue
    0xFF, 0x82, 0x00, 0x4B,  # indigo
    0xFF, 0xFF, 0x00, 0x8B,  # violet
])

HARDWARE_MODES = [RGBMode.OFF, RGBMode.STATIC, RGBMode.BREATHING,
                  RGBMode.COLOR_CYCLE, RGBMode.RAINBOW]


class CreativeKatanaController(RGBController):
    """Controller for the Creative Sound BlasterX Katana soundbar."""

    def __init__(self, dev, firmware: str, profile: int):
        self._dev = dev
        self._firmware = firmware
        self._profile = profile
        self._current_mode = RGBMode.STATIC
        self._current_speed = 3
        self._color_correction = (1.0, 1.0, 1.0)
        # Zone colors indexed 1-7; zone 0 / None sets all of them
        self._zone_colors: dict[int, tuple[int, int, int]] = {
            z: (255, 255, 255) for z in range(1, NUM_GROUPS + 1)
        }
        # Cache of state already written to the device, to skip redundant writes
        self._written: dict = {}

        self._zones = [RGBZone(0, "All Zones")] + [
            RGBZone(z, f"Zone {z}") for z in range(1, NUM_GROUPS + 1)
        ]

    # -- Low-level protocol --

    def _write_frame(self, cmd: int, data: bytes) -> None:
        frame = bytes([MAGIC, cmd, len(data)]) + bytes(data)
        # hidapi wants a leading report ID (0x00 = unnumbered), frame padded
        buf = (b"\x00" + frame).ljust(PACKET_SIZE + 1, b"\x00")
        if self._dev.write(buf) < 0:
            raise IOError("Katana HID write failed")

    def _read_frame(self, timeout_ms: int = 500) -> bytes | None:
        data = self._dev.read(PACKET_SIZE, timeout_ms)
        return bytes(data) if data else None

    def _lighting_set(self, data: bytes) -> None:
        """Send a lighting set command and wait for its 0x02 ack."""
        self._write_frame(CMD_LIGHTING, data)
        sub = data[0]
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            resp = self._read_frame()
            if resp is None:
                continue
            # Ack frame: 5a 02 <len> 3a <code> <sub> <echo...>
            if resp[1] == CMD_ERROR and resp[3] == CMD_LIGHTING and resp[5] == sub:
                if resp[4] != 0x00:
                    raise IOError(
                        f"Katana rejected lighting sub 0x{sub:02X} "
                        f"(code 0x{resp[4]:02X})"
                    )
                return
            # Async events (volume, input, ...) — ignore
        log.debug("Katana: no ack for lighting sub 0x%02X", sub)

    def _lighting_get(self, data: bytes) -> bytes | None:
        """Send a lighting query, return the reply's data bytes (after echo)."""
        self._write_frame(CMD_LIGHTING, data)
        sub = data[0]
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            resp = self._read_frame()
            if resp is None:
                continue
            if resp[1] == CMD_LIGHTING and resp[3] == sub:
                return resp[3:3 + resp[2]]
            if resp[1] == CMD_ERROR and resp[3] == CMD_LIGHTING and resp[5] == sub:
                return None
        return None

    # -- Cached device writes --

    def _set_if_changed(self, key: str, value, writer) -> None:
        if self._written.get(key) != value:
            writer()
            self._written[key] = value

    def _write_type(self, ptype: int) -> None:
        self._set_if_changed(
            "type", ptype,
            lambda: self._lighting_set(bytes([SUB_SET_TYPE, self._profile, ptype])),
        )

    def _write_pattern(self, ptype: int, groups: list[int]) -> None:
        """groups: palette slot per LED group (7 entries, expanded to 49 LEDs)."""
        slots = bytes(s for s in groups for _ in range(NUM_LEDS // NUM_GROUPS))
        ngroups = len(set(groups))
        self._set_if_changed(
            ("pattern", ptype), bytes(groups),
            lambda: self._lighting_set(
                bytes([SUB_SET_PATTERN, 0x00, ptype, ngroups, 0x00]) + slots
            ),
        )

    def _write_palette(self, ptype: int, start_slot: int, entries: bytes,
                       prefix: int = 0x00) -> None:
        self._set_if_changed(
            ("palette", ptype, start_slot), entries,
            lambda: self._lighting_set(
                bytes([SUB_SET_PARAM, prefix, ptype, PARAM_PALETTE, start_slot])
                + entries
            ),
        )

    def _write_speed(self, ptype: int) -> None:
        period = SPEED_PERIODS[max(0, min(5, self._current_speed))]
        self._set_if_changed(
            ("speed", ptype), period,
            lambda: self._lighting_set(
                bytes([SUB_SET_PARAM, 0x00, ptype, PARAM_SPEED,
                       period & 0xFF, period >> 8])
            ),
        )

    def _write_lights(self, on: bool) -> None:
        self._set_if_changed(
            "lights", on,
            lambda: self._lighting_set(bytes([SUB_ONOFF, 0x01 if on else 0x00])),
        )

    # -- RGBController interface --

    @property
    def name(self) -> str:
        return "Sound BlasterX Katana"

    @property
    def zones(self) -> list[RGBZone]:
        return self._zones

    @property
    def supported_modes(self) -> list[RGBMode]:
        return list(HARDWARE_MODES)

    @property
    def has_hardware_mode(self) -> bool:
        return self._current_mode in HARDWARE_MODES

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
        color = self._correct_color(r, g, b)
        if zone is None or zone == 0:
            for z in self._zone_colors:
                self._zone_colors[z] = color
        elif zone in self._zone_colors:
            self._zone_colors[zone] = color

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        self._current_mode = mode
        self._current_speed = speed

    def apply(self) -> None:
        mode = self._current_mode

        if mode == RGBMode.OFF:
            self._write_lights(False)
            return

        if mode == RGBMode.STATIC:
            colors = list(self._zone_colors.values())
            if all(c == colors[0] for c in colors):
                # Solo: every LED on palette slot 1
                self._write_type(TYPE_STATIC)
                self._write_pattern(TYPE_STATIC, [1] * NUM_GROUPS)
                r, g, b = colors[0]
                self._write_palette(TYPE_STATIC, 1, bytes([0xFF, b, g, r]))
            else:
                # Mood: LED groups on slots 2-8, one color each
                self._write_type(TYPE_STATIC)
                self._write_pattern(TYPE_STATIC,
                                    list(range(2, NUM_GROUPS + 2)))
                entries = b"".join(
                    bytes([0xFF, b, g, r]) for r, g, b in colors
                )
                self._write_palette(TYPE_STATIC, 2, entries)

        elif mode == RGBMode.BREATHING:
            self._write_type(TYPE_PULSATE)
            self._write_speed(TYPE_PULSATE)
            r, g, b = next(iter(self._zone_colors.values()))
            try:
                # Pulsate only accepts palette writes with the 0x01 prefix byte
                self._write_palette(TYPE_PULSATE, 1, bytes([0xFF, b, g, r]),
                                    prefix=0x01)
            except IOError as e:
                # Fall back to the device's stored pulsate color
                log.debug("Katana: pulsate color not accepted: %s", e)

        elif mode == RGBMode.COLOR_CYCLE:
            self._write_type(TYPE_CYCLE)
            self._write_pattern(TYPE_CYCLE, list(range(1, NUM_GROUPS + 1)))
            self._write_palette(TYPE_CYCLE, 1, RAINBOW_PALETTE)
            self._write_speed(TYPE_CYCLE)

        elif mode == RGBMode.RAINBOW:
            self._write_type(TYPE_WAVE)
            self._write_pattern(TYPE_WAVE, list(range(1, NUM_GROUPS + 1)))
            self._write_palette(TYPE_WAVE, 1, RAINBOW_PALETTE)
            self._write_speed(TYPE_WAVE)

        self._write_lights(True)

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None


def detect_creative_katana() -> CreativeKatanaController | None:
    """Detect a Creative Sound BlasterX Katana soundbar."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping Katana detection")
        return None

    for dev_info in hid.enumerate(KATANA_VID, KATANA_PID):
        # The command channel is the 64-byte HID interface. hidapi's hidraw
        # backend reports interface_number reliably; verify by probing anyway.
        iface = dev_info.get("interface_number", -1)
        if iface not in (COMMAND_INTERFACE, -1):
            continue

        path = dev_info.get("path", b"")
        try:
            dev = hid.device()
            dev.open_path(path)

            probe = CreativeKatanaController(dev, "", 0)
            # Firmware version: cmd 0x07, data 0x02 -> ASCII string
            probe._write_frame(CMD_SYSINFO, bytes([0x02]))
            firmware = ""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                resp = probe._read_frame()
                if resp is None:
                    continue
                if resp[1] == CMD_SYSINFO:
                    firmware = resp[3:3 + resp[2]].split(b"\x00")[0].decode(
                        "ascii", errors="replace"
                    )
                    break
            if not firmware:
                dev.close()
                continue

            cur = probe._lighting_get(bytes([SUB_CUR_PROFILE]))
            profile = cur[1] if cur and len(cur) > 1 else 0

            probe._firmware = firmware
            probe._profile = profile
            log.info("Found Sound BlasterX Katana (fw %s, profile %d)",
                     firmware, profile)
            return probe

        except Exception as e:
            log.debug("Katana candidate rejected: %s", e)
            continue

    return None
