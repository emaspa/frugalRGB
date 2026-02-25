import json
import logging
import os
import subprocess
import sys
import tempfile
import threading

import customtkinter as ctk
import pystray
from CTkColorPicker import AskColor
from PIL import Image, ImageDraw, ImageFilter

from ..controllers.base import RGBController, RGBMode
from ..diagnostics import collect_diagnostics
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
        self._app_icon = self._create_app_icon()
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
        bottom_bar.pack(side="bottom", fill="x", padx=10, pady=(0, 4))

        diag_btn = ctk.CTkButton(
            bottom_bar, text="Diagnostics", width=90, height=24,
            font=ctk.CTkFont(size=11), fg_color="gray30", hover_color="gray40",
            command=self._run_diagnostics,
        )
        diag_btn.pack(side="left")

        version_label = ctk.CTkLabel(
            bottom_bar, text="v0.02", text_color="gray", font=ctk.CTkFont(size=11),
        )
        version_label.pack(side="right")

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

        # Device cards
        devices_frame = ctk.CTkScrollableFrame(self, height=120)
        devices_frame.pack(fill="x", padx=15, pady=5)

        self._device_cards: list[tuple[DeviceCard, RGBController]] = []
        if self._controllers:
            for ctrl in self._controllers:
                zone_tuples = [(z.zone_id, z.name) for z in ctrl.zones]
                card = DeviceCard(devices_frame, ctrl.name, zone_tuples)
                card.pack(fill="x", padx=5, pady=3)
                self._device_cards.append((card, ctrl))
        else:
            no_devices = ctk.CTkLabel(
                devices_frame,
                text="No RGB devices detected.\nMake sure you're running as Administrator/root.",
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
            entry = ctk.CTkEntry(color_frame, textvariable=var, width=45)
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
        for card, _ctrl in self._device_cards:
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

    def _get_zone_map(self) -> dict:
        """Return zone_map for all devices."""
        zone_map = {}
        for card, ctrl in self._device_cards:
            zone_id = card.selected_zone_id
            if zone_id is not None:
                zone_map[id(ctrl)] = zone_id
        return zone_map

    def _get_color_map(self) -> dict:
        """Return per-device color map from card indicators."""
        return {id(ctrl): card.current_color for card, ctrl in self._device_cards}

    def _apply(self) -> None:
        r, g, b = self._current_color
        effect = self._effect_selector.selected_effect
        speed = self._effect_selector.speed

        if effect == "off":
            self._turn_off()
            return

        zone_map = self._get_zone_map()
        color_map = self._get_color_map()
        log.info("Applying: effect=%s color=(%d,%d,%d) speed=%.1f", effect, r, g, b, speed)
        self._engine.start_effect(effect, r, g, b, speed,
                                  zone_map=zone_map, color_map=color_map)

    def _save_to_hardware(self) -> None:
        """Save current color/mode to DRAM NV flash with double confirmation."""
        saveable = [
            (card, ctrl) for card, ctrl in self._device_cards
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
        zone_map = self._get_zone_map()
        self._engine.turn_off(zone_map=zone_map)
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
            # Reset all zone dropdowns so preset applies to all LEDs/zones
            for card, _ctrl in self._device_cards:
                card.reset_zone()
            self._on_color_selected(r, g, b, all_devices=True)
            if "effect" in data:
                self._effect_selector.set_effect(data["effect"])
            if "speed" in data:
                self._effect_selector.set_speed(data["speed"])
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
        presets[name] = {
            "color": list(self._current_color),
            "effect": self._effect_selector.selected_effect,
            "speed": self._effect_selector.speed,
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

    @staticmethod
    def _create_app_icon(size: int = 256) -> Image.Image:
        """Generate RGB Venn diagram icon with transparent background."""
        r_ch = Image.new("L", (size, size), 0)
        g_ch = Image.new("L", (size, size), 0)
        b_ch = Image.new("L", (size, size), 0)

        cx, cy = size // 2, size // 2
        radius = int(size * 0.32)
        spread = int(size * 0.16)

        # Red = top, Green = bottom-left, Blue = bottom-right
        circles = [
            (r_ch, cx, cy - spread),
            (g_ch, cx - int(spread * 0.87), cy + int(spread * 0.5)),
            (b_ch, cx + int(spread * 0.87), cy + int(spread * 0.5)),
        ]
        for ch, ox, oy in circles:
            ImageDraw.Draw(ch).ellipse(
                [ox - radius, oy - radius, ox + radius, oy + radius], fill=255,
            )

        # Additive merge: overlaps naturally produce C/M/Y/W
        rgb = Image.merge("RGB", (r_ch, g_ch, b_ch))

        # Soft glow: blend with a blurred version
        glow = rgb.filter(ImageFilter.GaussianBlur(radius=size // 16))
        rgb = Image.blend(rgb, glow, 0.4)

        # Alpha from brightness — black becomes transparent
        alpha = rgb.convert("L")
        img = rgb.convert("RGBA")
        img.putalpha(alpha)
        return img

    def _set_window_icon(self) -> None:
        """Set the taskbar/window icon via Win32 API."""
        self._ico_path = os.path.join(tempfile.gettempdir(), "frugalrgb_icon.ico")
        sizes = [(16, 16), (32, 32), (48, 48), (256, 256)]
        self._app_icon.save(self._ico_path, format="ICO", sizes=sizes)
        try:
            self.iconbitmap(self._ico_path)
        except Exception:
            pass
        # Force icon via SendMessage WM_SETICON after window is mapped
        self.after(50, self._apply_win32_icon)

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
        self._tray_icon = pystray.Icon(
            "frugalRGB", self._app_icon, "frugalRGB",
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

    def _apply_quit_exit(self) -> None:
        """Exit after --apply-quit: stop engine but keep LEDs on."""
        self._engine.stop()
        self.destroy()

    def _quit_app(self) -> None:
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
