import customtkinter as ctk


class ColorPresetBar(ctk.CTkFrame):
    """Row of quick-access color preset buttons."""

    PRESETS = [
        ("Red", "#FF0000"),
        ("Green", "#00FF00"),
        ("Blue", "#0000FF"),
        ("Cyan", "#00FFFF"),
        ("Purple", "#8000FF"),
        ("Orange", "#FF8000"),
        ("White", "#FFFFFF"),
        ("Warm", "#FFB060"),
        ("Off", "#000000"),
    ]

    def __init__(self, master, on_color_select, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._callback = on_color_select

        for name, hex_color in self.PRESETS:
            btn = ctk.CTkButton(
                self,
                text=name,
                width=60,
                height=30,
                fg_color=hex_color if hex_color != "#000000" else "#333333",
                text_color="white" if hex_color not in ("#FFFFFF", "#FFB060", "#00FF00") else "black",
                hover_color=hex_color if hex_color != "#000000" else "#555555",
                command=lambda c=hex_color: self._on_click(c),
            )
            btn.pack(side="left", padx=2, pady=2)

    def _on_click(self, hex_color: str) -> None:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        self._callback(r, g, b)


class DeviceCard(ctk.CTkFrame):
    """Card displaying a detected RGB device with zone selector."""

    def __init__(self, master, device_name: str, zones: list[tuple[int, str]], **kwargs):
        """zones: list of (zone_id, zone_name) tuples."""
        super().__init__(master, corner_radius=8, **kwargs)
        self._zone_map: dict[str, int | None] = {"All Zones": None}
        for zone_id, zone_name in zones:
            self._zone_map[zone_name] = zone_id

        has_zones = len(zones) > 1

        # Single row: checkbox + name + color indicator (+ zone dropdown if needed)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=10, pady=(6, 6), anchor="w")

        self._enabled_var = ctk.BooleanVar(value=True)
        self._enable_cb = ctk.CTkCheckBox(
            row, text="", variable=self._enabled_var, width=24,
        )
        self._enable_cb.pack(side="left", padx=(0, 5))

        self._name_label = ctk.CTkLabel(
            row, text=device_name, font=ctk.CTkFont(size=13, weight="bold")
        )
        self._name_label.pack(side="left", padx=(0, 10))

        self._color = (0, 0, 0)
        self._color_indicator = ctk.CTkFrame(row, width=18, height=18, corner_radius=4)
        self._color_indicator.pack(side="left", padx=(0, 10))
        self._set_indicator_color(0, 0, 0)

        zone_names = [name for _, name in zones]
        if has_zones:
            self._zone_var = ctk.StringVar(value="All Zones")
            zone_options = ["All Zones"] + zone_names
            self._zone_menu = ctk.CTkOptionMenu(
                row, variable=self._zone_var, values=zone_options, width=150
            )
            self._zone_menu.pack(side="left")
        else:
            self._zone_var = ctk.StringVar(value=zone_names[0] if zone_names else "Default")

    def _set_indicator_color(self, r: int, g: int, b: int) -> None:
        hex_color = f"#{r:02X}{g:02X}{b:02X}"
        self._color_indicator.configure(fg_color=hex_color)

    def update_color(self, r: int, g: int, b: int) -> None:
        self._color = (r, g, b)
        self._set_indicator_color(r, g, b)

    @property
    def current_color(self) -> tuple[int, int, int]:
        return self._color

    @property
    def selected_zone(self) -> str:
        return self._zone_var.get()

    @property
    def selected_zone_id(self) -> int | None:
        return self._zone_map.get(self._zone_var.get())

    @property
    def enabled(self) -> bool:
        return self._enabled_var.get()

    def reset_zone(self) -> None:
        """Reset zone dropdown to 'All Zones'."""
        self._zone_var.set("All Zones")


class EffectSelector(ctk.CTkFrame):
    """Dropdown for effect mode + speed slider."""

    EFFECTS = ["Static", "Breathing", "Color Cycle", "Rainbow", "Strobe", "Off"]

    def __init__(self, master, on_effect_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._callback = on_effect_change

        label = ctk.CTkLabel(self, text="Effect:")
        label.pack(side="left", padx=(0, 5))

        self._effect_var = ctk.StringVar(value="Static")
        self._effect_menu = ctk.CTkOptionMenu(
            self,
            variable=self._effect_var,
            values=self.EFFECTS,
            width=130,
            command=self._on_effect_selected,
        )
        self._effect_menu.pack(side="left", padx=5)

        speed_label = ctk.CTkLabel(self, text="Speed:")
        speed_label.pack(side="left", padx=(15, 5))

        self._speed_var = ctk.DoubleVar(value=1.0)
        self._speed_slider = ctk.CTkSlider(
            self,
            from_=0.2,
            to=3.0,
            variable=self._speed_var,
            width=120,
        )
        self._speed_slider.pack(side="left", padx=5)

    def _on_effect_selected(self, *_) -> None:
        if self._callback:
            self._callback()

    @property
    def selected_effect(self) -> str:
        return self._effect_var.get().lower().replace(" ", "_")

    @property
    def speed(self) -> float:
        return self._speed_var.get()

    def set_effect(self, effect: str) -> None:
        display = effect.replace("_", " ").title()
        if display in self.EFFECTS:
            self._effect_var.set(display)

    def set_speed(self, speed: float) -> None:
        self._speed_var.set(speed)


class CalibrationPanel(ctk.CTkFrame):
    """Per-device RGB calibration sliders with device selector."""

    def __init__(self, master, device_names: list[str], on_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._callback = on_change
        self._device_names = device_names
        # Store per-device corrections: {device_name: (R, G, B)}
        self._corrections: dict[str, tuple[float, float, float]] = {
            name: (1.0, 1.0, 1.0) for name in device_names
        }
        self._vars: dict[str, ctk.DoubleVar] = {}

        header = ctk.CTkLabel(self, text="Calibration", font=ctk.CTkFont(size=13, weight="bold"))
        header.grid(row=0, column=0, sticky="w", pady=(0, 4))

        # Device selector
        if len(device_names) > 1:
            self._device_var = ctk.StringVar(value=device_names[0])
            dev_menu = ctk.CTkOptionMenu(
                self, variable=self._device_var, values=device_names,
                width=200, command=self._on_device_switch,
            )
            dev_menu.grid(row=0, column=1, columnspan=5, sticky="w", padx=(10, 0), pady=(0, 4))
        else:
            self._device_var = ctk.StringVar(value=device_names[0] if device_names else "")

        for col, ch in enumerate(["R", "G", "B"]):
            var = ctk.DoubleVar(value=1.0)
            self._vars[ch] = var

            lbl = ctk.CTkLabel(self, text=ch, width=15)
            lbl.grid(row=1, column=col * 2, padx=(0, 2))

            slider = ctk.CTkSlider(
                self, from_=0.1, to=1.0, variable=var, width=100,
                command=self._on_slide,
            )
            slider.grid(row=1, column=col * 2 + 1, padx=(0, 10))

        self._value_label = ctk.CTkLabel(self, text=self._format_values(), width=120)
        self._value_label.grid(row=1, column=6, padx=(5, 0))

    def _format_values(self) -> str:
        r = self._vars["R"].get()
        g = self._vars["G"].get()
        b = self._vars["B"].get()
        return f"{r:.0%}  {g:.0%}  {b:.0%}"

    def _on_device_switch(self, *_) -> None:
        """Load the selected device's calibration into the sliders."""
        name = self._device_var.get()
        r, g, b = self._corrections.get(name, (1.0, 1.0, 1.0))
        self._vars["R"].set(r)
        self._vars["G"].set(g)
        self._vars["B"].set(b)
        self._value_label.configure(text=self._format_values())

    def _on_slide(self, *_) -> None:
        # Save current slider values to the selected device
        name = self._device_var.get()
        self._corrections[name] = (
            self._vars["R"].get(),
            self._vars["G"].get(),
            self._vars["B"].get(),
        )
        self._value_label.configure(text=self._format_values())
        if self._callback:
            self._callback()

    def get_correction(self, device_name: str) -> tuple[float, float, float]:
        return self._corrections.get(device_name, (1.0, 1.0, 1.0))

    def set_corrections(self, corrections: dict[str, tuple[float, float, float]]) -> None:
        """Load all per-device corrections (e.g. from config file)."""
        for name, val in corrections.items():
            if name in self._corrections:
                self._corrections[name] = val
        # Refresh sliders for currently selected device
        self._on_device_switch()
