"""Diagnostics collector for frugalRGB — gathers device info into a zip for support."""

import io
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import zipfile

log = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".frugalrgb_config.json")
PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".frugalrgb_presets.json")


def collect_diagnostics(controllers, bus=None, log_capture: str = "") -> str:
    """Collect all diagnostics into a zip on the user's Desktop.

    Returns the path to the created zip file.
    """
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    zip_path = os.path.join(desktop, "frugalrgb_diagnostics.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("system_info.txt", _system_info())
        zf.writestr("devices.txt", _device_discovery(controllers, bus))

        # Per-controller detail dumps
        for ctrl in controllers:
            cls_name = type(ctrl).__name__
            if cls_name == "ASRockPolychromeUSBController":
                zf.writestr("asrock_polychrome.txt", _asrock_detail(ctrl))
            elif cls_name == "ENEDDR5Controller":
                addr = getattr(ctrl, "_addr", 0)
                zf.writestr(f"ene_ddr5_0x{addr:02X}.txt", _ene_ddr5_detail(ctrl))
            elif cls_name == "MSIMysticLightController":
                pid = getattr(ctrl, "_pid", 0)
                zf.writestr(f"msi_mystic_0x{pid:04X}.txt", _msi_detail(ctrl))
            elif cls_name == "GigabyteRGBFusion2Controller":
                pid = getattr(ctrl, "_pid", 0)
                zf.writestr(f"gigabyte_rgb_0x{pid:04X}.txt",
                            _gigabyte_detail(ctrl))
            elif cls_name == "ASUSGPUController":
                zf.writestr("asus_gpu.txt", _asus_gpu_detail(ctrl))

        # Config files
        for path, arcname in [(CONFIG_FILE, "config.json"), (PRESETS_FILE, "presets.json")]:
            if os.path.exists(path):
                zf.write(path, arcname)

        # Captured log output
        if log_capture:
            zf.writestr("app.log", log_capture)

    log.info("Diagnostics saved to %s", zip_path)
    return zip_path


def _system_info() -> str:
    lines = ["=== System Info ===", ""]

    lines.append(f"OS: {platform.platform()}")
    lines.append(f"Architecture: {platform.machine()}")
    lines.append(f"Python: {sys.version}")
    lines.append(f"Frozen (exe): {getattr(sys, 'frozen', False)}")

    # Admin check
    is_admin = False
    if sys.platform == "win32":
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            pass
    else:
        is_admin = os.geteuid() == 0
    lines.append(f"Admin/root: {is_admin}")
    lines.append("")

    # Library versions
    lines.append("--- Library versions ---")
    for lib in ["customtkinter", "pystray", "hid", "PIL", "CTkColorPicker"]:
        try:
            mod = __import__(lib)
            ver = getattr(mod, "__version__", getattr(mod, "version", "unknown"))
            lines.append(f"  {lib}: {ver}")
        except ImportError:
            lines.append(f"  {lib}: NOT INSTALLED")
    lines.append("")

    # PawnIO check (Windows only)
    if sys.platform == "win32":
        pawnio_path = r"C:\Program Files\PawnIO\PawnIOLib.dll"
        lines.append(f"PawnIO DLL: {'found' if os.path.exists(pawnio_path) else 'NOT FOUND'}")
        module_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "modules"
        )
        smbus_bin = os.path.join(module_dir, "SmbusI801.bin")
        lines.append(f"SmbusI801.bin: {'found' if os.path.exists(smbus_bin) else 'NOT FOUND'}")
        lines.append("")

    return "\n".join(lines)


def _device_discovery(controllers, bus) -> str:
    lines = ["=== Device Discovery ===", ""]

    # USB HID enumeration
    lines.append("--- All USB HID devices ---")
    try:
        import hid
        for dev in hid.enumerate():
            vid = dev.get("vendor_id", 0)
            pid = dev.get("product_id", 0)
            product = dev.get("product_string", "") or ""
            manufacturer = dev.get("manufacturer_string", "") or ""
            path = (dev.get("path", b"") or b"")
            if isinstance(path, bytes):
                path = path.decode("utf-8", errors="replace")
            lines.append(
                f"  VID={vid:04X} PID={pid:04X}  {manufacturer} {product}"
            )
    except ImportError:
        lines.append("  hidapi not installed")
    except Exception as e:
        lines.append(f"  Error enumerating HID: {e}")
    lines.append("")

    # SMBus scan
    lines.append("--- SMBus scan (0x50-0x77) ---")
    if bus is not None:
        for addr in range(0x50, 0x78):
            try:
                val = bus.read_byte_data(addr, 0x00)
                lines.append(f"  0x{addr:02X}: responded (byte0=0x{val:02X})")
            except Exception:
                pass  # No device at this address, skip silently
    else:
        lines.append("  SMBus not available (not admin or init failed)")
    lines.append("")

    # Detected controllers summary
    lines.append("--- Detected controllers ---")
    if controllers:
        for ctrl in controllers:
            cls_name = type(ctrl).__name__
            zones = ", ".join(z.name for z in ctrl.zones)
            modes = ", ".join(m.name for m in ctrl.supported_modes)
            lines.append(f"  {ctrl.name}")
            lines.append(f"    Type: {cls_name}")
            lines.append(f"    Zones: {zones}")
            lines.append(f"    Modes: {modes}")

            if hasattr(ctrl, "_addr"):
                lines.append(f"    SMBus addr: 0x{ctrl._addr:02X}")
            if hasattr(ctrl, "_path"):
                lines.append(f"    HID path: {ctrl._path}")
    else:
        lines.append("  No controllers detected")
    lines.append("")

    return "\n".join(lines)


def _asrock_detail(ctrl) -> str:
    """Dump ASRock Polychrome USB config tables and zone readbacks."""
    lines = ["=== ASRock Polychrome USB Detail ===", ""]

    lines.append(f"Device: {ctrl.name}")
    if hasattr(ctrl, "_path"):
        lines.append(f"HID path: {ctrl._path}")
    lines.append("")

    REPORT_SIZE = 65

    # Read config tables
    for cfg_id, cfg_name in [(0x01, "Zone Availability"), (0x02, "LED Count"), (0x03, "RGSwap")]:
        try:
            buf = [0x00] * REPORT_SIZE
            buf[0x01] = 0x14  # CMD_READ_HEADER
            buf[0x03] = cfg_id
            resp = ctrl._hid_write_read(buf)
            if resp:
                hex_dump = " ".join(f"{b:02X}" for b in resp[:16])
                lines.append(f"Config 0x{cfg_id:02X} ({cfg_name}): {hex_dump}")
            else:
                lines.append(f"Config 0x{cfg_id:02X} ({cfg_name}): no response")
        except Exception as e:
            lines.append(f"Config 0x{cfg_id:02X} ({cfg_name}): error - {e}")
    lines.append("")

    # Per-zone readback
    lines.append("--- Per-zone readback ---")
    for zone_id in range(8):
        try:
            buf = [0x00] * REPORT_SIZE
            buf[0x01] = 0x11  # CMD_READ_ZONE
            buf[0x03] = zone_id
            resp = ctrl._hid_write_read(buf)
            if resp:
                hex_dump = " ".join(f"{b:02X}" for b in resp[:16])
                lines.append(f"  Zone {zone_id}: {hex_dump}")
            else:
                lines.append(f"  Zone {zone_id}: no response")
        except Exception as e:
            lines.append(f"  Zone {zone_id}: error - {e}")
    lines.append("")

    return "\n".join(lines)


def _ene_ddr5_detail(ctrl) -> str:
    """Dump ENE DDR5 DRAM registers."""
    lines = ["=== ENE DDR5 DRAM Detail ===", ""]

    addr = getattr(ctrl, "_addr", 0)
    lines.append(f"Device: {ctrl.name}")
    lines.append(f"SMBus address: 0x{addr:02X}")
    lines.append("")

    # Device name string
    lines.append("--- Device name (0x1000) ---")
    name_chars = []
    for i in range(16):
        val = ctrl._read_register(0x1000 + i)
        if val is None or val == 0:
            break
        name_chars.append(f"{val:02X}({chr(val) if 0x20 <= val < 0x7F else '?'})")
    lines.append(f"  {'  '.join(name_chars)}")
    lines.append("")

    # Config table dump (0x1C00-0x1C1F)
    lines.append("--- Config table (0x1C00-0x1C1F) ---")
    row = []
    for i in range(0x20):
        val = ctrl._read_register(0x1C00 + i)
        row.append(f"{val:02X}" if val is not None else "??")
        if (i + 1) % 16 == 0:
            offset = 0x1C00 + i - 15
            lines.append(f"  0x{offset:04X}: {' '.join(row)}")
            row = []
    lines.append("")

    # Mode/speed/direction registers (0x8020-0x802F)
    lines.append("--- Control registers (0x8020-0x802F) ---")
    reg_names = {
        0x8020: "Direct Select", 0x8021: "Mode", 0x8022: "Speed",
        0x8023: "Direction",
    }
    for reg in range(0x8020, 0x8030):
        val = ctrl._read_register(reg)
        name = reg_names.get(reg, "")
        val_str = f"0x{val:02X}" if val is not None else "??"
        lines.append(f"  0x{reg:04X}: {val_str}  {name}")
    lines.append("")

    # V2 color registers (0x8160-0x817F)
    lines.append("--- V2 effect color registers (0x8160-0x817F) ---")
    row = []
    for i in range(0x20):
        val = ctrl._read_register(0x8160 + i)
        row.append(f"{val:02X}" if val is not None else "??")
        if (i + 1) % 16 == 0:
            offset = 0x8160 + i - 15
            lines.append(f"  0x{offset:04X}: {' '.join(row)}")
            row = []
    lines.append("")

    # LED count
    num_leds = getattr(ctrl, "_num_leds", "?")
    lines.append(f"LED count: {num_leds}")
    lines.append("")

    return "\n".join(lines)


def _msi_detail(ctrl) -> str:
    """Dump MSI Mystic Light 185-byte state packet."""
    lines = ["=== MSI Mystic Light Detail ===", ""]

    pid = getattr(ctrl, "_pid", 0)
    lines.append(f"Device: {ctrl.name}")
    lines.append(f"PID: 0x{pid:04X}")
    if hasattr(ctrl, "_path"):
        lines.append(f"HID path: {ctrl._path}")
    lines.append(f"Product string: {getattr(ctrl, '_product_string', '?')}")
    lines.append(f"Onboard LEDs: {getattr(ctrl, '_onboard_leds', '?')}")
    lines.append("")

    # Full state hex dump
    state = getattr(ctrl, "_state", None)
    if state:
        lines.append(f"--- Full state ({len(state)} bytes) ---")
        for row_start in range(0, len(state), 16):
            chunk = state[row_start:row_start + 16]
            hex_str = " ".join(f"{b:02X}" for b in chunk)
            lines.append(f"  {row_start:3d}: {hex_str}")
        lines.append("")

        # Parse zone data
        from .controllers.msi_mystic_light import ZONE_DEFS
        lines.append("--- Zone data ---")
        mode_names = {
            0x00: "Disable", 0x01: "Static", 0x02: "Breathing",
            0x03: "Flashing", 0x04: "Double Flashing", 0x05: "Lightning",
            0x15: "Color Shift", 0x19: "Rainbow",
        }
        for name, zdef in ZONE_DEFS.items():
            offset = zdef["offset"]
            effect = state[offset]
            r, g, b = state[offset+1], state[offset+2], state[offset+3]
            flags = state[offset+4]
            brightness = (flags >> 2) & 0x1F
            speed = flags & 0x03
            color_flags = state[offset+8]
            mode_str = mode_names.get(effect, f"0x{effect:02X}")
            lines.append(
                f"  {name:16s}: mode={mode_str:12s} "
                f"RGB=({r:3d},{g:3d},{b:3d}) "
                f"bright={brightness:2d} speed={speed} "
                f"flags=0x{color_flags:02X}"
            )
        lines.append(f"  save_data: {state[184]}")
        lines.append("")

    return "\n".join(lines)


def _gigabyte_detail(ctrl) -> str:
    """Dump Gigabyte RGB Fusion 2.0 device info."""
    lines = ["=== Gigabyte RGB Fusion 2.0 Detail ===", ""]

    lines.append(f"Device: {ctrl.name}")
    pid = getattr(ctrl, "_pid", 0)
    lines.append(f"PID: 0x{pid:04X}")
    if hasattr(ctrl, "_path"):
        lines.append(f"HID path: {ctrl._path}")
    lines.append(f"Product string: {getattr(ctrl, '_product_string', '?')}")
    lines.append(f"Device name: {getattr(ctrl, '_device_name', '?')}")
    lines.append(f"FW version: {getattr(ctrl, '_fw_version', '?')}")
    lines.append(f"Chip ID: 0x{getattr(ctrl, '_chip_id', 0):08X}")
    lines.append(f"IT5711 mode: {getattr(ctrl, '_is_5711', False)}")
    lines.append("")

    # Re-read device info for raw dump
    try:
        ctrl._send_cmd(0x60)
        info = ctrl._recv_packet()
        if info:
            lines.append(f"--- Device info report ({len(info)} bytes) ---")
            for row_start in range(0, len(info), 16):
                chunk = info[row_start:row_start + 16]
                hex_str = " ".join(f"{b:02X}" for b in chunk)
                lines.append(f"  {row_start:3d}: {hex_str}")
            lines.append("")

            # Parse key fields
            if len(info) >= 10:
                lines.append("--- Parsed fields ---")
                lines.append(f"  product byte: 0x{info[1]:02X}")
                lines.append(f"  device_num: {info[2]}")
                lines.append(f"  strip_detect: 0x{info[3]:02X}")
                lines.append(f"  fw_ver: {'.'.join(str(b) for b in info[4:8])}")
                lines.append(f"  led_count_high: 0x{info[8]:02X}")
                lines.append(f"  led_count_low: 0x{info[9]:02X}")
                if len(info) >= 12:
                    lines.append(f"  strip_ctrl_length1: {info[10]}")
                    lines.append(f"  support_cmd_flag: 0x{info[11]:02X}")
                lines.append("")
    except Exception as e:
        lines.append(f"Failed to read device info: {e}")
        lines.append("")

    # Zone layout
    lines.append("--- Zone layout ---")
    zone_leds = getattr(ctrl, "_zone_leds", [])
    for z in ctrl.zones:
        led_idx = zone_leds[z.zone_id] if z.zone_id < len(zone_leds) else None
        lines.append(f"  Zone {z.zone_id}: {z.name} (LED index={led_idx})")
    lines.append("")

    return "\n".join(lines)


def _asus_gpu_detail(ctrl) -> str:
    """Dump ASUS GPU ENE RGB controller info."""
    lines = ["=== ASUS GPU RGB Detail ===", ""]

    lines.append(f"Device: {ctrl.name}")
    lines.append(f"GPU: {getattr(ctrl, '_gpu_name', '?')}")
    lines.append(f"Subsystem: {getattr(ctrl, '_subsys_name', '?')}")
    lines.append(f"ENE device: {getattr(ctrl, '_device_name', '?')}")
    lines.append(f"LED count: {getattr(ctrl, '_num_leds', '?')}")
    lines.append("")

    # Config table dump (0x1C00-0x1C1F)
    lines.append("--- Config table (0x1C00-0x1C1F) ---")
    row = []
    for i in range(0x20):
        val = ctrl._read_register(0x1C00 + i)
        row.append(f"{val:02X}" if val is not None else "??")
        if (i + 1) % 16 == 0:
            offset = 0x1C00 + i - 15
            lines.append(f"  0x{offset:04X}: {' '.join(row)}")
            row = []
    lines.append("")

    # Control registers
    lines.append("--- Control registers ---")
    reg_names = {
        0x8020: "Direct Select", 0x8021: "Mode", 0x8022: "Speed",
        0x8023: "Direction",
    }
    for reg in range(0x8020, 0x8024):
        val = ctrl._read_register(reg)
        name = reg_names.get(reg, "")
        lines.append(f"  0x{reg:04X}: 0x{val:02X}  {name}" if val is not None
                     else f"  0x{reg:04X}: ??  {name}")
    lines.append("")

    # Effect color registers
    num_leds = getattr(ctrl, "_num_leds", 1)
    lines.append(f"--- Effect colors (0x8160, {num_leds} LEDs) ---")
    for i in range(num_leds):
        base = 0x8160 + i * 3
        r = ctrl._read_register(base)
        b = ctrl._read_register(base + 1)
        g = ctrl._read_register(base + 2)
        if r is not None and b is not None and g is not None:
            lines.append(f"  LED {i}: R={r:3d} G={g:3d} B={b:3d}")
        else:
            lines.append(f"  LED {i}: ??")
    lines.append("")

    # Zones
    lines.append("--- Zones ---")
    for z in ctrl.zones:
        lines.append(f"  Zone {z.zone_id}: {z.name}")
    lines.append("")

    return "\n".join(lines)
