"""ASUS Aura USB HID motherboard RGB controller (VID 0x0B05, mainboard protocol).

Faithful port of OpenRGB's ``AuraMainboardController`` (used by most AMD/Intel
ASUS boards from X570/B550 onwards, including the B760M BTF — issue #2).

Protocol notes (these are the things the previous implementation got wrong):

* The device uses HID **output reports** (``dev.write``), *not* feature reports.
* Every packet is 65 bytes and the **first byte is 0xEC** (it acts as the report
  ID). The command byte is at offset 0x01. The old code prepended a 0x00 report
  ID which shifted everything one byte to the right.
* Zones/LED counts are not fixed — they are read from a 60-byte **config table**
  reported by the device. Onboard LEDs, RGB headers and addressable headers all
  come from that table.
* Onboard ("fixed") lighting is driven with the effect (0x35) + effect-colour
  (0x36) commands; addressable headers are driven with the direct (0x40) command.
"""

import logging

from .base import RGBController, RGBMode, RGBZone

log = logging.getLogger(__name__)

ASUS_AURA_VID = 0x0B05

# Mainboard PIDs (OpenRGB: AURA_MOTHERBOARD_1/2/3). 0x19AF is the controller on
# recent boards such as the ASUS B760M BTF.
KNOWN_PIDS = {0x19AF}
# Reported on older boards but not verified against this implementation.
EXPERIMENTAL_PIDS = {0x1939, 0x18F3}

REPORT_SIZE = 65          # report-ID byte (0xEC) + 64 data bytes
AURA_PREFIX = 0xEC        # first byte of every packet

# Request commands
AURA_REQUEST_FIRMWARE = 0x82
AURA_REQUEST_CONFIG = 0xB0

# Control commands
AURA_EFFECT = 0x35        # set effect mode for a channel
AURA_EFFECT_COLOR = 0x36  # set effect colour for onboard (fixed) LEDs
AURA_COMMIT = 0x3F        # commit / save
AURA_DIRECT = 0x40        # direct per-LED control (addressable headers)
AURA_GEN1 = (0x52, 0x53, 0x00, 0x01)  # "Gen1" init handshake

# Hardware mode values
AURA_MODE_OFF = 0x00
AURA_MODE_STATIC = 0x01
AURA_MODE_BREATHING = 0x02
AURA_MODE_FLASHING = 0x03
AURA_MODE_SPECTRUM_CYCLE = 0x04
AURA_MODE_RAINBOW = 0x05
AURA_MODE_DIRECT = 0xFF

LEDS_PER_PACKET = 0x14    # 20 LEDs per direct packet

# Config-table reply IDs
REPLY_FIRMWARE = 0x02
REPLY_CONFIG = 0x30

# Onboard colour packets carry a 16-bit LED mask, so a single effect-colour
# packet can address at most 16 onboard LEDs.
MAX_ONBOARD_LEDS = 16

# Addressable headers don't report their strip length; fill this many LEDs when
# applying a solid colour. Extra LEDs beyond the physical strip are ignored.
DEFAULT_ADDRESSABLE_LEDS = 60

# frugalrgb mode -> Aura hardware mode value
AURA_MODE_MAP = {
    RGBMode.OFF: AURA_MODE_OFF,
    RGBMode.STATIC: AURA_MODE_STATIC,
    RGBMode.BREATHING: AURA_MODE_BREATHING,
    RGBMode.STROBE: AURA_MODE_FLASHING,
    RGBMode.COLOR_CYCLE: AURA_MODE_SPECTRUM_CYCLE,
    RGBMode.RAINBOW: AURA_MODE_RAINBOW,
}

# Modes the hardware animates on its own (the software effect loop leaves these
# to the device instead of streaming frames).
HARDWARE_ANIMATED_MODES = {
    RGBMode.BREATHING, RGBMode.STROBE, RGBMode.COLOR_CYCLE, RGBMode.RAINBOW,
}

# Device types within the config table
TYPE_FIXED = "fixed"
TYPE_ADDRESSABLE = "addressable"


def _pad(data: list[int]) -> list[int]:
    """Pad/truncate a packet to REPORT_SIZE bytes."""
    return (list(data) + [0x00] * REPORT_SIZE)[:REPORT_SIZE]


class AsusAuraUSBController(RGBController):
    """ASUS Aura mainboard RGB via USB HID (VID 0x0B05)."""

    def __init__(self, dev, device_path: str, pid: int, product_string: str):
        self._dev = dev
        self._path = device_path
        self._pid = pid
        self._product_string = (product_string or "").strip()
        self._current_mode = RGBMode.STATIC
        self._current_speed = 3
        self._color_correction = (1.0, 1.0, 1.0)
        self._firmware = ""
        # channel index -> (r, g, b)
        self._pending_colors: dict[int, tuple[int, int, int]] = {}
        # last colours actually committed to the device (for save-to-hardware)
        self._applied_colors: dict[int, tuple[int, int, int]] = {}
        # list of dicts: effect, direct, num_leds, start_led, type
        self._devices: list[dict] = []

        self._firmware = self._read_firmware()
        config = self._read_config_table()
        self._config_ok = config is not None
        self._build_devices(config)
        self._send_gen1()
        self._setup_zones()

    # ------------------------------------------------------------------ #
    #  Low-level HID                                                       #
    # ------------------------------------------------------------------ #

    def _write(self, data: list[int]) -> None:
        """Send a 65-byte output report (report ID 0xEC + 64 data bytes)."""
        self._dev.write(_pad([AURA_PREFIX] + list(data)))

    def _query(self, request: int, reply_id: int,
               data_offset: int, length: int) -> list[int] | None:
        """Send a request packet and return ``length`` payload bytes of the reply.

        The reply is ``[0xEC, reply_id, ...]`` and the payload starts
        ``data_offset`` bytes after the reply-id byte (1 for firmware, 3 for the
        config table — matching OpenRGB). Some hidapi backends strip the leading
        report-ID byte, so the reply id is located by scanning rather than by a
        fixed index.
        """
        self._write([request])
        try:
            resp = self._dev.read(REPORT_SIZE, timeout_ms=1000)
        except Exception as e:
            log.debug("ASUS Aura read failed (request 0x%02X): %s", request, e)
            return None
        if not resp:
            return None
        resp = list(resp)

        # Locate the reply id. Normal framing puts it at index 1 (after the
        # 0xEC report-id byte); a stripped report puts it at index 0.
        for idx in (1, 0):
            if idx < len(resp) and resp[idx] == reply_id:
                start = idx + data_offset
                return resp[start:start + length]
        log.debug("ASUS Aura unexpected reply for request 0x%02X: %s",
                  request, resp[:8])
        return None

    # ------------------------------------------------------------------ #
    #  Init                                                                #
    # ------------------------------------------------------------------ #

    def _read_firmware(self) -> str:
        # Firmware string sits 1 byte after the reply id (OpenRGB: usb_buf[2]).
        payload = self._query(AURA_REQUEST_FIRMWARE, REPLY_FIRMWARE,
                              data_offset=1, length=16)
        if not payload:
            return "unknown"
        raw = bytes(b & 0xFF for b in payload)
        text = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        return text or "unknown"

    def _read_config_table(self) -> list[int] | None:
        # Config table sits 3 bytes after the reply id (OpenRGB: usb_buf[4]).
        config = self._query(AURA_REQUEST_CONFIG, REPLY_CONFIG,
                             data_offset=3, length=60)
        if config and len(config) >= 0x1E:
            log.debug("ASUS Aura config table: %s",
                      " ".join(f"{b:02X}" for b in config))
            return config
        log.warning("ASUS Aura: could not read config table (PID=0x%04X) — "
                    "falling back to a single onboard zone. Is Armoury Crate / "
                    "ASUS LightingService still running?", self._pid)
        return None

    def _build_devices(self, config: list[int] | None) -> None:
        """Derive the per-channel device list from the config table.

        Mirrors OpenRGB's AuraMainboardController constructor:
          config[0x1B] = total onboard LEDs
          config[0x1D] = number of RGB headers (subset of onboard LEDs)
          config[0x02] = number of addressable headers
        """
        if config is None:
            # Conservative fallback so the device still registers and can be
            # exercised by the diagnostic script. One small onboard zone.
            self._devices = [{
                "effect": 0, "direct": 0x04, "num_leds": 1,
                "start_led": 0, "type": TYPE_FIXED,
            }]
            return

        num_onboard_leds = config[0x1B]
        num_rgb_headers = config[0x1D]
        num_addressable = config[0x02]
        if num_onboard_leds < num_rgb_headers:
            num_rgb_headers = 0

        effect_channel = 0
        start_led = 0
        if num_onboard_leds > 0:
            self._devices.append({
                "effect": effect_channel,
                "direct": 0x04,
                "num_leds": num_onboard_leds,
                "num_headers": num_rgb_headers,
                "start_led": start_led,
                "type": TYPE_FIXED,
            })
            effect_channel += 1
            start_led += num_onboard_leds

        for i in range(num_addressable):
            self._devices.append({
                "effect": effect_channel,
                "direct": i,
                "num_leds": DEFAULT_ADDRESSABLE_LEDS,
                "num_headers": 0,
                "start_led": 0,
                "type": TYPE_ADDRESSABLE,
            })
            effect_channel += 1

        if not self._devices:
            self._devices.append({
                "effect": 0, "direct": 0x04, "num_leds": 1,
                "start_led": 0, "type": TYPE_FIXED,
            })

        log.info("ASUS Aura: %d onboard LED(s), %d RGB header(s), "
                 "%d addressable header(s)",
                 num_onboard_leds, num_rgb_headers, num_addressable)

    def _send_gen1(self) -> None:
        self._write(list(AURA_GEN1))

    def _setup_zones(self) -> None:
        # Zone 0 is a convenience "all zones" master; zones 1..N map to channels.
        self._zones = [RGBZone(0, "All Zones")]
        addr_n = 0
        for i, dev in enumerate(self._devices):
            if dev["type"] == TYPE_FIXED:
                name = "Onboard RGB"
            else:
                addr_n += 1
                name = f"Addressable Header {addr_n}"
            self._zones.append(RGBZone(i + 1, name))

    # ------------------------------------------------------------------ #
    #  Packet builders                                                     #
    # ------------------------------------------------------------------ #

    def _send_effect(self, channel: int, mode: int, shutdown: bool = False) -> None:
        self._write([AURA_EFFECT, channel, 0x00, 0x01 if shutdown else 0x00, mode])

    def _send_color(self, start_led: int, count: int,
                    rgb: tuple[int, int, int], shutdown: bool = False) -> None:
        count = max(0, min(count, MAX_ONBOARD_LEDS - start_led))
        if count <= 0:
            return
        mask = ((1 << count) - 1) << start_led
        packet = [AURA_EFFECT_COLOR, (mask >> 8) & 0xFF, mask & 0xFF,
                  0x01 if shutdown else 0x00]
        packet += [0x00] * (3 * start_led)
        packet += list(rgb) * count
        self._write(packet)

    def _send_direct(self, device: int, colors: list[tuple[int, int, int]]) -> None:
        total = len(colors)
        offset = 0
        while True:
            sent = min(LEDS_PER_PACKET, total - offset)
            apply_flag = 0x80 if (offset + sent >= total) else 0x00
            packet = [AURA_DIRECT, apply_flag | device, offset, sent]
            for i in range(sent):
                packet += list(colors[offset + i])
            self._write(packet)
            offset += sent
            if offset >= total:
                break

    def _send_commit(self) -> None:
        self._write([AURA_COMMIT, 0x55])

    # ------------------------------------------------------------------ #
    #  RGBController interface                                             #
    # ------------------------------------------------------------------ #

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
    def supports_hardware_save(self) -> bool:
        return True

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
            for idx in range(len(self._devices)):
                self._pending_colors[idx] = (r, g, b)
        else:
            idx = zone - 1
            if 0 <= idx < len(self._devices):
                self._pending_colors[idx] = (r, g, b)

    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        # Aura mainboard effects carry no speed field; speed is stored for parity
        # but not transmitted.
        self._current_mode = mode
        self._current_speed = speed

    def apply(self) -> None:
        if not self._pending_colors:
            return
        mode_val = AURA_MODE_MAP.get(self._current_mode, AURA_MODE_STATIC)
        self._apply_channels(self._pending_colors, mode_val, shutdown=False)
        self._send_commit()
        self._applied_colors.update(self._pending_colors)
        self._pending_colors.clear()

    def _apply_channels(self, colors: dict[int, tuple[int, int, int]],
                        mode_val: int, shutdown: bool) -> None:
        for ch_idx, (r, g, b) in colors.items():
            dev = self._devices[ch_idx]
            if dev["type"] == TYPE_FIXED:
                self._send_effect(dev["effect"], mode_val, shutdown)
                if mode_val != AURA_MODE_OFF:
                    self._send_color(dev["start_led"], dev["num_leds"],
                                     (r, g, b), shutdown)
            else:  # addressable header
                if mode_val in (AURA_MODE_OFF, AURA_MODE_STATIC):
                    # Solid colour via direct per-LED control.
                    self._send_effect(dev["effect"], AURA_MODE_DIRECT, shutdown)
                    fill = (0, 0, 0) if mode_val == AURA_MODE_OFF else (r, g, b)
                    self._send_direct(dev["direct"], [fill] * dev["num_leds"])
                else:
                    # Let the hardware animate the strip.
                    self._send_effect(dev["effect"], mode_val, shutdown)

    def save_to_hardware(self) -> None:
        """Persist the current colour/mode so it survives a power cycle."""
        mode_val = AURA_MODE_MAP.get(self._current_mode, AURA_MODE_STATIC)
        # Re-apply the last committed colours with the shutdown/save flag set so
        # the device stores them as the boot effect, then commit.
        colors = self._pending_colors or self._applied_colors
        if not colors:
            return
        self._apply_channels(dict(colors), mode_val, shutdown=True)
        self._send_commit()

    def close(self) -> None:
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None


def detect_asus_aura_usb() -> "AsusAuraUSBController | None":
    """Detect an ASUS Aura USB mainboard RGB controller."""
    try:
        import hid
    except ImportError:
        log.debug("hidapi not installed, skipping ASUS Aura USB detection")
        return None

    all_pids = KNOWN_PIDS | EXPERIMENTAL_PIDS
    # The device can expose several HID interfaces; only one answers the config
    # query. Keep the first that opens as a fallback, but prefer one whose config
    # table read succeeds.
    fallback: "AsusAuraUSBController | None" = None

    for dev_info in hid.enumerate(ASUS_AURA_VID):
        pid = dev_info.get("product_id", 0)
        if pid not in all_pids:
            continue

        product = dev_info.get("product_string", "") or ""
        path = dev_info.get("path", b"")
        path_str = (path.decode("utf-8", errors="replace")
                    if isinstance(path, bytes) else str(path))

        if pid in EXPERIMENTAL_PIDS:
            log.warning("ASUS Aura PID 0x%04X is experimental — please report "
                        "results", pid)

        try:
            dev = hid.device()
            dev.open_path(path)
            controller = AsusAuraUSBController(dev, path_str, pid, product)
        except Exception as e:
            log.warning("Failed to open ASUS Aura USB (PID=0x%04X): %s — is "
                        "Armoury Crate / ASUS LightingService running?", pid, e)
            continue

        if controller._config_ok:
            log.info("Found ASUS Aura USB at %s (PID=0x%04X, fw=%s, %s)",
                     path_str, pid, controller._firmware, product.strip())
            if fallback is not None and fallback is not controller:
                fallback.close()
            return controller

        # Couldn't read the config table on this interface — remember it but
        # keep looking for a better one.
        if fallback is None:
            fallback = controller
        else:
            controller.close()

    if fallback is not None:
        log.info("Found ASUS Aura USB (PID=0x%04X) but could not read its config "
                 "table — registering with a single onboard zone", fallback._pid)
    return fallback
