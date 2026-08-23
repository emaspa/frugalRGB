import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

import customtkinter as ctk
import pystray
from CTkColorPicker import AskColor
from PIL import Image

from ..controllers.base import RGBController, RGBMode
from ..controllers.asus_aura_usb import AsusAuraUSBController
from ..diagnostics import collect_diagnostics
from ..icon import create_app_icon
from ..effects.engine import EffectEngine
from .widgets import CalibrationPanel, ColorPresetBar, DeviceCard, EffectSelector

log = logging.getLogger(__name__)

PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".frugalrgb_presets.json")
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".frugalrgb_config.json")


class FrugalRGBApp(ctk.CTk):
    """Main application window."""

    def __init__(self, controllers: list[RGBController], apply_quit: bool = False, bus=None):
        super().__init__()

        self._controllers = controllers
        self._bus = bus
        self._engine = EffectEngine()
        self._engine.set_controllers(controllers)
        self._current_color: tuple[int, int, int] = (255, 255, 255)
        self._apply_quit = apply_quit

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("frugalRGB - no bloat, just LEDs")
        w, h = 700, 600
        sx = self.winfo_screenwidth() // 2 - w // 2
        sy = self.winfo_screenheight() // 2 - h // 2
        self.geometry(f"{w}x{h}+{sx}+{sy}")
        self.minsize(500, 520)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._tray_icon: pystray.Icon | None = None
        self._app_icon = create_app_icon()
        self._set_window_icon()

        self._build_ui()
        self._load_config()
        self._refresh_preset_menu()
        if not self._apply_quit:
            self._init_tray()
        self._apply_startup_preset()

        if self._start_minimized_var.get():
            self.withdraw()

    def _build_ui(self) -> None:
        # --- Pack bottom sections FIRST so Tkinter reserves their space ---

        # Bottom bar (diagnostics + version)
        bottom_bar = ctk.CTkFrame(self, fg_color="transparent")
        # Extra bottom/right margin keeps the version label clear of the
        # rounded window corners some compositors draw (KDE on Wayland).
        bottom_bar.pack(side="bottom", fill="x", padx=10, pady=(0, 8))

        diag_btn = ctk.CTkButton(
            bottom_bar, text="Diagnostics", width=90, height=24,
            font=ctk.CTkFont(size=11), fg_color="gray30", hover_color="gray40",
            command=self._run_diagnostics,
        )
        diag_btn.pack(side="left")

        aura_btn = ctk.CTkButton(
            bottom_bar, text="Aura Test", width=80, height=24,
            font=ctk.CTkFont(size=11), fg_color="gray30", hover_color="gray40",
            command=self._run_aura_test,
        )
        aura_btn.pack(side="left", padx=(6, 0))

        version_label = ctk.CTkLabel(
            bottom_bar, text="v0.1.0", text_color="gray", font=ctk.CTkFont(size=11),
            height=24,
        )
        version_label.pack(side="right", padx=(0, 10))

        # Presets row
        preset_frame = ctk.CTkFrame(self, fg_color="transparent")
        preset_frame.pack(side="bottom", fill="x", padx=15, pady=(0, 6))

        preset_label = ctk.CTkLabel(preset_frame, text="Preset:")
        preset_label.pack(side="left", padx=(0, 5))

        self._preset_var = ctk.StringVar(value="")
        self._preset_menu = ctk.CTkOptionMenu(
            preset_frame, variable=self._preset_var,
            values=["(none)"], width=160,
            command=self._on_preset_selected,
        )
        self._preset_menu.pack(side="left", padx=5)

        save_btn = ctk.CTkButton(
            preset_frame, text="Save", width=60, command=self._save_preset
        )
        save_btn.pack(side="left", padx=5)

        del_btn = ctk.CTkButton(
            preset_frame, text="Delete", width=60, fg_color="#dc3545",
            hover_color="#c82333", command=self._delete_preset
        )
        del_btn.pack(side="left", padx=5)

        startup_label = ctk.CTkLabel(preset_frame, text="On start:")
        startup_label.pack(side="left", padx=(20, 5))

        self._startup_preset_var = ctk.StringVar(value="(none)")
        self._startup_preset_menu = ctk.CTkOptionMenu(
            preset_frame, variable=self._startup_preset_var,
            values=["(none)"], width=140,
            command=lambda _: self._save_config(),
        )
        self._startup_preset_menu.pack(side="left", padx=5)

        # Options row
        opts_frame = ctk.CTkFrame(self, fg_color="transparent")
        opts_frame.pack(side="bottom", fill="x", padx=15, pady=(0, 4))

        self._off_on_close_var = ctk.BooleanVar(value=False)
        self._off_on_close_cb = ctk.CTkCheckBox(
            opts_frame, text="Off on close", variable=self._off_on_close_var,
            width=100, command=self._save_config,
        )
        self._off_on_close_cb.pack(side="left", padx=5)

        self._minimize_to_tray_var = ctk.BooleanVar(value=False)
        minimize_to_tray_cb = ctk.CTkCheckBox(
            opts_frame, text="Close to tray", variable=self._minimize_to_tray_var,
            width=100, command=self._save_config,
        )
        minimize_to_tray_cb.pack(side="left", padx=5)

        self._start_minimized_var = ctk.BooleanVar(value=False)
        start_minimized_cb = ctk.CTkCheckBox(
            opts_frame, text="Start minimized", variable=self._start_minimized_var,
            width=110, command=self._save_config,
        )
        start_minimized_cb.pack(side="left", padx=5)

        self._start_at_login_var = ctk.BooleanVar(value=self._startup_shortcut_exists())
        start_at_login_cb = ctk.CTkCheckBox(
            opts_frame, text="Start at login", variable=self._start_at_login_var,
            width=100, command=self._toggle_start_at_login,
        )
        start_at_login_cb.pack(side="left", padx=5)

        # Bottom buttons (Apply / LEDs Off)
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="bottom", fill="x", padx=15, pady=(4, 4))

        apply_btn = ctk.CTkButton(
            btn_frame, text="Apply", width=100, fg_color="#28a745",
            hover_color="#218838", command=self._apply
        )
        apply_btn.pack(side="left", padx=5)

        off_btn = ctk.CTkButton(
            btn_frame, text="LEDs Off", width=80, fg_color="#dc3545",
            hover_color="#c82333", command=self._turn_off
        )
        off_btn.pack(side="left", padx=5)

        # Only show Save to Hardware if any controller supports it
        if any(ctrl.supports_hardware_save for ctrl in self._controllers):
            save_hw_btn = ctk.CTkButton(
                btn_frame, text="Save to Hardware", width=130,
                fg_color="#e67e22", hover_color="#d35400",
                command=self._save_to_hardware,
            )
            save_hw_btn.pack(side="right", padx=5)

        # --- Now pack top content (fills remaining space) ---

        # Header
        header = ctk.CTkLabel(
            self, text="frugalRGB - no bloat, just LEDs", font=ctk.CTkFont(size=22, weight="bold")
        )
        header.pack(pady=(12, 4))

        # Device cards — one card per zone for independent control
        devices_frame = ctk.CTkScrollableFrame(self, height=120)
        devices_frame.pack(fill="both", expand=True, padx=15, pady=5)

        # Each entry: (card, controller, zone_id)
        self._device_cards: list[tuple[DeviceCard, RGBController, int | None]] = []
        if self._controllers:
            for ctrl in self._controllers:
                zones = ctrl.zones
                if len(zones) <= 1:
                    # Single zone — one card, no zone suffix
                    card = DeviceCard(devices_frame, ctrl.name)
                    card.pack(fill="x", padx=5, pady=3)
                    zone_id = zones[0].zone_id if zones else None
                    self._device_cards.append((card, ctrl, zone_id))
                else:
                    # Multiple zones — one card per zone
                    for z in zones:
                        label = f"{ctrl.name} — {z.name}"
                        card = DeviceCard(devices_frame, label)
                        card.pack(fill="x", padx=5, pady=3)
                        self._device_cards.append((card, ctrl, z.zone_id))
        else:
            if sys.platform == "win32":
                no_devices_hint = "Make sure you're running as Administrator."
            else:
                no_devices_hint = (
                    "Install the udev rules (70-frugalrgb.rules) for USB devices\n"
                    "and join the 'i2c' group for DRAM RGB."
                )
            no_devices = ctk.CTkLabel(
                devices_frame,
                text=f"No RGB devices detected.\n{no_devices_hint}",
                text_color="gray",
            )
            no_devices.pack(pady=20)

        # Color picker button + current color display
        color_frame = ctk.CTkFrame(self, fg_color="transparent")
        color_frame.pack(fill="x", padx=15, pady=5)

        self._color_display = ctk.CTkFrame(
            color_frame, width=40, height=40, corner_radius=6
        )
        self._color_display.pack(side="left", padx=(0, 10))
        self._update_color_display()

        pick_btn = ctk.CTkButton(
            color_frame, text="Pick Color", width=100, command=self._open_color_picker
        )
        pick_btn.pack(side="left", padx=5)

        # RGB entry fields
        self._r_var = ctk.StringVar(value="255")
        self._g_var = ctk.StringVar(value="255")
        self._b_var = ctk.StringVar(value="255")

        for label_text, var in [("R", self._r_var), ("G", self._g_var), ("B", self._b_var)]:
            lbl = ctk.CTkLabel(color_frame, text=label_text, width=15)
            lbl.pack(side="left", padx=(10, 2))
            entry = ctk.CTkEntry(color_frame, textvariable=var, width=60)
            entry.pack(side="left", padx=2)

        rgb_apply_btn = ctk.CTkButton(
            color_frame, text="Set", width=40, command=self._apply_rgb_entry
        )
        rgb_apply_btn.pack(side="left", padx=5)

        # Preset bar
        self._presets = ColorPresetBar(self, on_color_select=self._on_color_selected)
        self._presets.pack(fill="x", padx=15, pady=5)

        # Effect selector
        self._effect_selector = EffectSelector(self, on_effect_change=self._on_effect_change)
        self._effect_selector.pack(fill="x", padx=15, pady=5)

        # Calibration sliders (per-device)
        device_names = [ctrl.name for ctrl in self._controllers] or ["(none)"]
        self._calibration = CalibrationPanel(
            self, device_names=device_names, on_change=self._on_calibration_change
        )
        self._calibration.pack(fill="x", padx=15, pady=5)


    def _update_color_display(self) -> None:
        r, g, b = self._current_color
        self._color_display.configure(fg_color=f"#{r:02X}{g:02X}{b:02X}")

    def _open_color_picker(self) -> None:
        color = AskColor()
        result = color.get()
        if result:
            # AskColor returns hex string like "#RRGGBB"
            hex_str = result.lstrip("#")
            r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
            self._on_color_selected(r, g, b)

    def _apply_rgb_entry(self) -> None:
        try:
            r = max(0, min(255, int(self._r_var.get())))
            g = max(0, min(255, int(self._g_var.get())))
            b = max(0, min(255, int(self._b_var.get())))
            self._on_color_selected(r, g, b)
        except ValueError:
            pass

    def _on_color_selected(self, r: int, g: int, b: int,
                           all_devices: bool = False) -> None:
        self._current_color = (r, g, b)
        self._r_var.set(str(r))
        self._g_var.set(str(g))
        self._b_var.set(str(b))
        self._update_color_display()
        for card, _ctrl, _zid in self._device_cards:
            if all_devices or card.enabled:
                card.update_color(r, g, b)


    def _on_calibration_change(self) -> None:
        for ctrl in self._controllers:
            if hasattr(ctrl, "color_correction"):
                ctrl.color_correction = self._calibration.get_correction(ctrl.name)
        self._save_config()
        self._apply()

    def _on_effect_change(self) -> None:
        # Auto-apply when effect changes
        self._apply()

    def _get_entries(self) -> list[tuple]:
        """Return list of (ctrl, zone_id, r, g, b) for all enabled cards."""
        entries = []
        for card, ctrl, zone_id in self._device_cards:
            if card.enabled:
                r, g, b = card.current_color
                entries.append((ctrl, zone_id, r, g, b))
        return entries

    def _apply(self) -> None:
        effect = self._effect_selector.selected_effect
        speed = self._effect_selector.speed

        if effect == "off":
            self._turn_off()
            return

        entries = self._get_entries()
        log.info("Applying: effect=%s speed=%.1f entries=%d", effect, speed, len(entries))
        self._engine.start_effect(effect, speed, entries)

    def _save_to_hardware(self) -> None:
        """Save current color/mode to DRAM NV flash with double confirmation."""
        saveable = [
            (card, ctrl) for card, ctrl, _zid in self._device_cards
            if ctrl.supports_hardware_save
        ]
        if not saveable:
            return

        names = "\n".join(f"  - {ctrl.name}" for _, ctrl in saveable)

        # First confirmation — explain the risk
        dlg1 = ctk.CTkToplevel(self)
        dlg1.title("Save to Hardware")
        dlg1.resizable(False, False)
        dw, dh = 480, 260
        x = self.winfo_x() + self.winfo_width() // 2 - dw // 2
        y = self.winfo_y() + self.winfo_height() // 2 - dh // 2
        dlg1.geometry(f"{dw}x{dh}+{x}+{y}")
        dlg1.transient(self)
        dlg1.grab_set()

        result = {"confirmed": False}

        warning_text = (
            "This will write the current color and mode to the\n"
            "non-volatile flash memory on your RAM sticks:\n\n"
            f"{names}\n\n"
            "The saved settings will persist across power cycles\n"
            "(boot color). This operation is known to be unstable\n"
            "on some ENE firmware versions and may in rare cases\n"
            "soft-lock the RGB controller, requiring a DIMM reseat\n"
            "to recover.\n\n"
            "Make sure you have already Applied the desired color."
        )
        ctk.CTkLabel(
            dlg1, text=warning_text, justify="left",
            font=ctk.CTkFont(size=12),
        ).pack(padx=20, pady=(15, 10))

        btn_frame1 = ctk.CTkFrame(dlg1, fg_color="transparent")
        btn_frame1.pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkButton(
            btn_frame1, text="Cancel", width=100, command=dlg1.destroy,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame1, text="I understand, continue", width=180,
            fg_color="#e67e22", hover_color="#d35400",
            command=lambda: _first_ok(),
        ).pack(side="right", padx=5)

        def _first_ok():
            result["confirmed"] = True
            dlg1.destroy()

        dlg1.wait_window()
        if not result["confirmed"]:
            return

        # Second confirmation — final "are you sure"
        dlg2 = ctk.CTkToplevel(self)
        dlg2.title("Final Confirmation")
        dlg2.resizable(False, False)
        dw2, dh2 = 500, 140
        x2 = self.winfo_x() + self.winfo_width() // 2 - dw2 // 2
        y2 = self.winfo_y() + self.winfo_height() // 2 - dh2 // 2
        dlg2.geometry(f"{dw2}x{dh2}+{x2}+{y2}")
        dlg2.transient(self)
        dlg2.grab_set()

        result2 = {"confirmed": False}

        ctk.CTkLabel(
            dlg2,
            text="Are you absolutely sure?\nThis writes to hardware flash and cannot be undone easily.",
            justify="center", font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(padx=20, pady=(15, 10))

        btn_frame2 = ctk.CTkFrame(dlg2, fg_color="transparent")
        btn_frame2.pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkButton(
            btn_frame2, text="Cancel", width=100, command=dlg2.destroy,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame2, text="Save to Hardware", width=150,
            fg_color="#dc3545", hover_color="#c82333",
            command=lambda: _second_ok(),
        ).pack(side="right", padx=5)

        def _second_ok():
            result2["confirmed"] = True
            dlg2.destroy()

        dlg2.wait_window()
        if not result2["confirmed"]:
            return

        # Perform the save
        for _card, ctrl in saveable:
            try:
                ctrl.save_to_hardware()
            except Exception as e:
                log.error("Failed to save to hardware on %s: %s", ctrl.name, e)

        # Success feedback
        dlg3 = ctk.CTkToplevel(self)
        dlg3.title("Saved")
        dlg3.resizable(False, False)
        dw3, dh3 = 300, 90
        x3 = self.winfo_x() + self.winfo_width() // 2 - dw3 // 2
        y3 = self.winfo_y() + self.winfo_height() // 2 - dh3 // 2
        dlg3.geometry(f"{dw3}x{dh3}+{x3}+{y3}")
        dlg3.transient(self)
        dlg3.grab_set()
        ctk.CTkLabel(dlg3, text="Settings saved to hardware flash.").pack(
            expand=True, padx=20, pady=(15, 5),
        )
        ctk.CTkButton(dlg3, text="OK", width=80, command=dlg3.destroy).pack(
            pady=(0, 10),
        )

    def _turn_off(self) -> None:
        log.info("Turning off all LEDs")
        entries = [(ctrl, zid, 0, 0, 0) for _card, ctrl, zid in self._device_cards]
        self._engine.turn_off(entries)
        self._on_color_selected(0, 0, 0, all_devices=True)

    def _load_presets_list(self) -> dict:
        """Load presets from file and return the dict."""
        try:
            if os.path.exists(PRESETS_FILE):
                with open(PRESETS_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _refresh_preset_menu(self) -> None:
        """Update the preset and startup preset dropdowns."""
        presets = self._load_presets_list()
        names = list(presets.keys()) if presets else []
        options = ["(none)"] + names
        self._preset_menu.configure(values=options)
        self._startup_preset_menu.configure(values=options)
        self._refresh_tray_menu()
        # If the selected startup preset was deleted, reset it
        if self._startup_preset_var.get() not in options:
            self._startup_preset_var.set("(none)")
            self._save_config()

    def _on_preset_selected(self, name: str) -> None:
        if name == "(none)":
            return
        presets = self._load_presets_list()
        if name in presets:
            data = presets[name]
            r, g, b = data["color"]

            if "effect" in data:
                self._effect_selector.set_effect(data["effect"])
            if "speed" in data:
                self._effect_selector.set_speed(data["speed"])

            device_states = data.get("devices", {})
            if device_states:
                for card, ctrl, zone_id in self._device_cards:
                    zones = ctrl.zones
                    zone_name = next((z.name for z in zones if z.zone_id == zone_id), "")
                    key = f"{ctrl.name}|{zone_name}" if len(zones) > 1 else ctrl.name
                    state = device_states.get(key)
                    if state:
                        card.set_enabled(state.get("enabled", True))
                        cr, cg, cb = state.get("color", [r, g, b])
                        card.update_color(cr, cg, cb)
                    else:
                        card.set_enabled(True)
                        card.update_color(r, g, b)
            else:
                # Legacy preset — single global color
                self._on_color_selected(r, g, b, all_devices=True)

            self._current_color = (r, g, b)
            self._update_color_display()
            self._preset_var.set(name)
            log.info("Loaded preset: %s", name)
            self._apply()

    def _save_preset(self) -> None:
        presets = self._load_presets_list()
        existing_names = list(presets.keys())

        dialog = ctk.CTkToplevel(self)
        dialog.title("Save Preset")
        dialog.resizable(False, False)
        dw = 320
        dh = 260 if existing_names else 140
        x = self.winfo_x() + self.winfo_width() // 2 - dw // 2
        y = self.winfo_y() + self.winfo_height() // 2 - dh // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        dialog.transient(self)
        dialog.grab_set()

        result = {"name": None}

        # Overwrite existing
        if existing_names:
            overwrite_label = ctk.CTkLabel(dialog, text="Overwrite existing:")
            overwrite_label.pack(padx=15, pady=(15, 4), anchor="w")
            current = self._preset_var.get()
            default = current if current in existing_names else existing_names[0]
            overwrite_var = ctk.StringVar(value=default)
            overwrite_menu = ctk.CTkOptionMenu(
                dialog, variable=overwrite_var, values=existing_names, width=280,
            )
            overwrite_menu.pack(padx=15, pady=(0, 6))
            overwrite_btn = ctk.CTkButton(
                dialog, text="Overwrite", width=280, height=32,
                command=lambda: _finish(overwrite_var.get()),
            )
            overwrite_btn.pack(padx=15, pady=(0, 10))

        # Or save as new
        new_label = ctk.CTkLabel(
            dialog, text="Or save as new:" if existing_names else "Preset name:",
        )
        new_label.pack(padx=15, pady=(10, 4), anchor="w")
        name_entry = ctk.CTkEntry(dialog, width=280, height=32, placeholder_text="New preset name")
        name_entry.pack(padx=15, pady=(0, 6))
        new_btn = ctk.CTkButton(
            dialog, text="Save New" if existing_names else "Save",
            width=280, height=32,
            command=lambda: _finish(name_entry.get()),
        )
        new_btn.pack(padx=15, pady=(0, 10))

        def _finish(name: str) -> None:
            if name:
                result["name"] = name
            dialog.destroy()

        dialog.wait_window()

        name = result["name"]
        if not name:
            return
        # Capture per-zone state (color, enabled)
        devices = {}
        for card, ctrl, zone_id in self._device_cards:
            zones = ctrl.zones
            zone_name = next((z.name for z in zones if z.zone_id == zone_id), "")
            key = f"{ctrl.name}|{zone_name}" if len(zones) > 1 else ctrl.name
            devices[key] = {
                "enabled": card.enabled,
                "color": list(card.current_color),
            }

        presets[name] = {
            "color": list(self._current_color),
            "effect": self._effect_selector.selected_effect,
            "speed": self._effect_selector.speed,
            "devices": devices,
        }
        try:
            with open(PRESETS_FILE, "w") as f:
                json.dump(presets, f, indent=2)
            log.info("Saved preset: %s", name)
            self._refresh_preset_menu()
            self._preset_var.set(name)
        except Exception as e:
            log.error("Failed to save preset: %s", e)

    def _delete_preset(self) -> None:
        name = self._preset_var.get()
        if not name or name == "(none)":
            return
        presets = self._load_presets_list()
        if name in presets:
            del presets[name]
            try:
                with open(PRESETS_FILE, "w") as f:
                    json.dump(presets, f, indent=2)
                log.info("Deleted preset: %s", name)
                self._preset_var.set("(none)")
                self._refresh_preset_menu()
            except Exception as e:
                log.error("Failed to delete preset: %s", e)

    def _save_config(self) -> None:
        try:
            calibration = {
                ctrl.name: list(self._calibration.get_correction(ctrl.name))
                for ctrl in self._controllers
            }
            data = {
                "calibration": calibration,
                "off_on_close": self._off_on_close_var.get(),
                "minimize_to_tray": self._minimize_to_tray_var.get(),
                "start_minimized": self._start_minimized_var.get(),
                "startup_preset": self._startup_preset_var.get(),
                "window_geometry": self.geometry(),
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("Failed to save config: %s", e)

    def _load_config(self) -> None:
        try:
            if not os.path.exists(CONFIG_FILE):
                self._on_calibration_change()
                return
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            if "calibration" in data:
                cal_data = data["calibration"]
                if isinstance(cal_data, dict):
                    corrections = {name: tuple(val) for name, val in cal_data.items()}
                    self._calibration.set_corrections(corrections)
                elif isinstance(cal_data, list):
                    # Old global format — apply to all devices
                    cal = tuple(cal_data)
                    corrections = {ctrl.name: cal for ctrl in self._controllers}
                    self._calibration.set_corrections(corrections)
                for ctrl in self._controllers:
                    if hasattr(ctrl, "color_correction"):
                        ctrl.color_correction = self._calibration.get_correction(ctrl.name)
            if "off_on_close" in data:
                self._off_on_close_var.set(data["off_on_close"])
            if "minimize_to_tray" in data:
                self._minimize_to_tray_var.set(data["minimize_to_tray"])
            if "start_minimized" in data:
                self._start_minimized_var.set(data["start_minimized"])
            if "startup_preset" in data:
                self._startup_preset_var.set(data["startup_preset"])
            if "window_geometry" in data:
                self.geometry(data["window_geometry"])
        except Exception as e:
            log.error("Failed to load config: %s", e)

    def _apply_startup_preset(self) -> None:
        """Apply the startup preset if one is configured."""
        name = self._startup_preset_var.get()
        if not name or name == "(none)":
            if self._apply_quit:
                self.after(500, self._apply_quit_exit)
            return
        self._on_preset_selected(name)
        if self._apply_quit:
            self.after(500, self._apply_quit_exit)

    def _set_window_icon(self) -> None:
        """Set the taskbar/window icon."""
        if sys.platform == "win32":
            self._ico_path = os.path.join(tempfile.gettempdir(), "frugalrgb_icon.ico")
            sizes = [(16, 16), (32, 32), (48, 48), (256, 256)]
            self._app_icon.save(self._ico_path, format="ICO", sizes=sizes)
            try:
                self.iconbitmap(self._ico_path)
            except Exception:
                pass
            # Force icon via SendMessage WM_SETICON after window is mapped
            self.after(50, self._apply_win32_icon)
        else:
            # Tk on Linux wants a PhotoImage, not an .ico
            png_path = os.path.join(tempfile.gettempdir(), "frugalrgb_icon.png")
            self._app_icon.save(png_path, format="PNG")
            try:
                import tkinter as tk
                self._icon_photo = tk.PhotoImage(file=png_path)  # keep a reference
                self.iconphoto(True, self._icon_photo)
            except Exception:
                pass

    def _apply_win32_icon(self) -> None:
        """Use Win32 SendMessage to force taskbar icon."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id())
            WM_SETICON = 0x0080
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            ico = self._ico_path
            icon_big = user32.LoadImageW(0, ico, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
            icon_small = user32.LoadImageW(0, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if icon_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, icon_big)
            if icon_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, icon_small)
        except Exception:
            pass

    @staticmethod
    def _startup_shortcut_path() -> str:
        startup = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        )
        return os.path.join(startup, "frugalRGB.lnk")

    def _startup_shortcut_exists(self) -> bool:
        return os.path.exists(self._startup_shortcut_path())

    def _toggle_start_at_login(self) -> None:
        if self._start_at_login_var.get():
            self._create_startup_shortcut()
        else:
            self._remove_startup_shortcut()

    def _ensure_scheduled_task(self) -> bool:
        """Create the scheduled task if it doesn't exist. Returns True if task is available."""
        # Check if task already exists
        result = subprocess.run(
            ["schtasks", "/query", "/tn", "frugalRGB"],
            capture_output=True, creationflags=0x08000000,
        )
        if result.returncode == 0:
            return True

        # Determine what to run
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
        else:
            import shutil
            pythonw = shutil.which("pythonw") or "pythonw.exe"
            main_pyw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "main.pyw")
            exe_path = f'{pythonw}" "{os.path.normpath(main_pyw)}'

        # Create the task (requires admin — which we should have)
        result = subprocess.run(
            ["schtasks", "/create", "/tn", "frugalRGB",
             "/tr", f'"{exe_path}"',
             "/sc", "ONCE", "/st", "00:00", "/rl", "HIGHEST", "/f"],
            capture_output=True, text=True, creationflags=0x08000000,
        )
        if result.returncode == 0:
            log.info("Created scheduled task 'frugalRGB'")
            return True
        log.error("Failed to create scheduled task: %s", result.stderr.strip())
        return False

    def _create_startup_shortcut(self) -> None:
        """Create a .lnk in the Startup folder."""
        lnk = self._startup_shortcut_path()

        if self._ensure_scheduled_task():
            target = "schtasks.exe"
            args = "/run /tn frugalRGB"
        elif getattr(sys, "frozen", False):
            target = sys.executable
            args = ""
        else:
            import shutil
            pythonw = shutil.which("pythonw") or "pythonw.exe"
            main_pyw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "main.pyw")
            main_pyw = os.path.normpath(main_pyw)
            target = pythonw
            args = main_pyw

        # Use single-quoted PowerShell strings to avoid escaping issues
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{lnk}'); "
            f"$s.TargetPath = '{target}'; "
            f"$s.Arguments = '{args}'; "
            "$s.WindowStyle = 7; "
            "$s.Save()"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, creationflags=0x08000000,
        )
        if result.returncode == 0:
            log.info("Created startup shortcut: %s", lnk)
        else:
            log.error("Failed to create startup shortcut: %s", result.stderr.strip())

    def _remove_startup_shortcut(self) -> None:
        lnk = self._startup_shortcut_path()
        try:
            if os.path.exists(lnk):
                os.remove(lnk)
                log.info("Removed startup shortcut: %s", lnk)
        except Exception as e:
            log.error("Failed to remove startup shortcut: %s", e)

    def _build_tray_menu(self) -> pystray.Menu:
        presets = self._load_presets_list()
        preset_items = []
        for name in presets:
            preset_items.append(
                pystray.MenuItem(name, self._make_tray_preset_action(name))
            )
        if not preset_items:
            preset_items.append(pystray.MenuItem("(none)", None, enabled=False))

        return pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Load Preset", pystray.Menu(*preset_items)),
            pystray.MenuItem("LEDs Off", self._tray_leds_off),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )

    def _init_tray(self) -> None:
        tray_image = self._app_icon
        if pystray.Icon.__module__ == "pystray._xorg":
            # KDE's xembedsniproxy chroma-keys legacy X11 tray icons with pure
            # green, so transparent pixels render as a green square. Flatten
            # onto an opaque dark tile so there is no transparency to key out.
            # The appindicator/gtk backends handle transparency natively.
            bg = Image.new("RGBA", tray_image.size, (30, 30, 30, 255))
            bg.alpha_composite(tray_image)
            tray_image = bg.convert("RGB")
        self._tray_icon = pystray.Icon(
            "frugalRGB", tray_image, "frugalRGB",
            self._build_tray_menu(),
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _refresh_tray_menu(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.menu = self._build_tray_menu()

    def _make_tray_preset_action(self, name: str):
        def action(icon, item):
            self.after(0, lambda: self._on_preset_selected(name))
        return action

    def _tray_show(self, icon=None, item=None) -> None:
        self.after(0, self._show_window)

    def _show_window(self) -> None:
        self.deiconify()
        self.after(50, self._apply_win32_icon)
        self.lift()
        self.focus_force()

    def _tray_leds_off(self, icon=None, item=None) -> None:
        self.after(0, self._turn_off)

    def _tray_quit(self, icon=None, item=None) -> None:
        self.after(0, self._quit_app)

    def _run_diagnostics(self) -> None:
        """Run diagnostics collector in a background thread."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Diagnostics")
        dialog.resizable(False, False)
        dw, dh = 300, 80
        x = self.winfo_x() + self.winfo_width() // 2 - dw // 2
        y = self.winfo_y() + self.winfo_height() // 2 - dh // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        dialog.transient(self)
        dialog.grab_set()

        status_label = ctk.CTkLabel(dialog, text="Collecting diagnostics...")
        status_label.pack(expand=True, padx=20, pady=20)

        def _collect():
            try:
                zip_path = collect_diagnostics(
                    self._controllers, bus=self._bus,
                )
                self.after(0, lambda: _done(zip_path))
            except Exception as e:
                log.error("Diagnostics failed: %s", e)
                self.after(0, lambda: _error(str(e)))

        def _done(path):
            dialog.destroy()
            result_dialog = ctk.CTkToplevel(self)
            result_dialog.title("Diagnostics")
            result_dialog.resizable(False, False)
            rw, rh = 400, 100
            rx = self.winfo_x() + self.winfo_width() // 2 - rw // 2
            ry = self.winfo_y() + self.winfo_height() // 2 - rh // 2
            result_dialog.geometry(f"{rw}x{rh}+{rx}+{ry}")
            result_dialog.transient(self)
            result_dialog.grab_set()
            msg = ctk.CTkLabel(result_dialog, text=f"Saved to:\n{path}", wraplength=360)
            msg.pack(expand=True, padx=20, pady=(15, 5))
            ok_btn = ctk.CTkButton(
                result_dialog, text="OK", width=80,
                command=result_dialog.destroy,
            )
            ok_btn.pack(pady=(0, 10))

        def _error(msg):
            dialog.destroy()
            err_dialog = ctk.CTkToplevel(self)
            err_dialog.title("Diagnostics Error")
            err_dialog.resizable(False, False)
            err_dialog.geometry(f"350x100+{self.winfo_x() + 175}+{self.winfo_y() + 240}")
            err_dialog.transient(self)
            err_dialog.grab_set()
            ctk.CTkLabel(err_dialog, text=f"Error: {msg}", wraplength=310).pack(
                expand=True, padx=20, pady=(15, 5),
            )
            ctk.CTkButton(err_dialog, text="OK", width=80, command=err_dialog.destroy).pack(
                pady=(0, 10),
            )

        threading.Thread(target=_collect, daemon=True).start()

    def _run_aura_test(self) -> None:
        """Open a dialog that shows the ASUS Aura controller info and cycles its
        LEDs through R/G/B/White so detection and protocol can be verified."""
        aura = next(
            (c for c in self._controllers if isinstance(c, AsusAuraUSBController)),
            None,
        )

        dialog = ctk.CTkToplevel(self)
        dialog.title("ASUS Aura Test")
        dialog.resizable(False, False)
        dw, dh = 420, 250
        x = self.winfo_x() + self.winfo_width() // 2 - dw // 2
        y = self.winfo_y() + self.winfo_height() // 2 - dh // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        dialog.transient(self)
        dialog.grab_set()

        if aura is None:
            ctk.CTkLabel(
                dialog,
                text=("No ASUS Aura controller detected.\n\n"
                      "If your board has ASUS Aura RGB, close Armoury Crate /\n"
                      "ASUS LightingService and restart frugalRGB. You can also\n"
                      "run test_asus_aura.py from a terminal for a raw scan."),
                justify="left", wraplength=380,
            ).pack(expand=True, padx=20, pady=(18, 8))
            ctk.CTkButton(dialog, text="Close", width=80,
                          command=dialog.destroy).pack(pady=(0, 12))
            return

        onboard = sum(d["num_leds"] for d in aura._devices
                      if d["type"] == "fixed")
        n_addr = sum(1 for d in aura._devices if d["type"] == "addressable")
        info = (
            f"{aura.name}\n"
            f"Firmware: {aura._firmware}\n"
            f"Config table read OK: {aura._config_ok}\n"
            f"Onboard LEDs: {onboard}    Addressable headers: {n_addr}\n"
            f"Zones: {len(aura.zones)}"
        )
        ctk.CTkLabel(dialog, text=info, justify="left", wraplength=390,
                     font=ctk.CTkFont(size=12)).pack(padx=20, pady=(16, 6))

        status_label = ctk.CTkLabel(dialog, text="Idle", text_color="gray")
        status_label.pack(pady=(2, 8))

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=(0, 12))

        def _set_status(text: str) -> None:
            self.after(0, lambda: status_label.configure(text=text))

        def _worker():
            sequence = [
                ("Red", (255, 0, 0)), ("Green", (0, 255, 0)),
                ("Blue", (0, 0, 255)), ("White", (255, 255, 255)),
            ]
            try:
                for name, (r, g, b) in sequence:
                    _set_status(f"Showing: {name}")
                    aura.set_mode(RGBMode.STATIC)
                    aura.set_color(r, g, b, zone=0)
                    aura.apply()
                    time.sleep(1.3)
                _set_status("Done — restoring current colors.")
                self.after(0, self._apply)
            except Exception as e:
                log.error("Aura test failed: %s", e)
                _set_status(f"Error: {e}")
            finally:
                self.after(0, lambda: start_btn.configure(state="normal"))

        def _start():
            start_btn.configure(state="disabled")
            # Stop the effect loop so it doesn't fight the test for the device.
            self._engine.stop()
            threading.Thread(target=_worker, daemon=True).start()

        start_btn = ctk.CTkButton(btn_row, text="Run R/G/B Test", width=130,
                                  command=_start)
        start_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Close", width=80, fg_color="gray30",
                      hover_color="gray40", command=dialog.destroy).pack(side="left")

    def _apply_quit_exit(self) -> None:
        """Exit after --apply-quit: stop engine but keep LEDs on."""
        self._engine.stop()
        self.destroy()

    def _quit_app(self) -> None:
        self._save_config()
        self._engine.stop()
        if self._off_on_close_var.get():
            for ctrl in self._controllers:
                ctrl.set_mode(RGBMode.OFF)
                ctrl.set_color(0, 0, 0)
                ctrl.apply()
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self.destroy()

    def _on_close(self) -> None:
        if self._minimize_to_tray_var.get():
            self.withdraw()
            return
        self._quit_app()
