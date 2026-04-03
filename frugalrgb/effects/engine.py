import colorsys
import math
import threading
import time

from ..controllers.base import RGBController, RGBMode

# Map effect names to RGBMode
EFFECT_MODE_MAP = {
    "off": RGBMode.OFF,
    "static": RGBMode.STATIC,
    "breathing": RGBMode.BREATHING,
    "color_cycle": RGBMode.COLOR_CYCLE,
    "rainbow": RGBMode.RAINBOW,
    "strobe": RGBMode.STROBE,
}


class EffectEngine:
    """Runs RGB effects — uses hardware modes when available, software fallback otherwise.

    Entries are (controller, zone_id, r, g, b) tuples. Multiple entries can
    reference the same controller with different zones/colors.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._controllers: list[RGBController] = []
        self._effect: str = "static"
        self._speed: float = 1.0
        self._entries: list[tuple] = []

    def set_controllers(self, controllers: list[RGBController]) -> None:
        self._controllers = controllers

    def start_effect(self, effect: str, speed: float,
                     entries: list[tuple]) -> None:
        """Start an effect.

        entries: list of (ctrl, zone_id, r, g, b) tuples.
        """
        self.stop()
        self._effect = effect
        self._speed = speed
        self._entries = list(entries)

        mode = EFFECT_MODE_MAP.get(effect, RGBMode.STATIC)

        # Apply once: group by controller for efficient set_mode/apply
        self._apply_entries(mode, entries)

        # Check if all controllers handle this mode in hardware
        ctrls_in_use = {id(e[0]) for e in entries}
        use_hardware = all(
            getattr(ctrl, "has_hardware_mode", False)
            for ctrl in self._controllers if id(ctrl) in ctrls_in_use
        ) if ctrls_in_use else True

        if effect in ("static", "off") or use_hardware:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=1.0)
            self._thread = None

    def turn_off(self, entries: list[tuple]) -> None:
        self.stop()
        mode = RGBMode.OFF
        off_entries = [(ctrl, zid, 0, 0, 0) for ctrl, zid, *_ in entries]
        self._apply_entries(mode, off_entries)

    def _apply_entries(self, mode: RGBMode,
                       entries: list[tuple]) -> None:
        """Group entries by controller, set mode once, colors per zone, apply once."""
        groups: dict[int, tuple[RGBController, list[tuple]]] = {}
        for ctrl, zone_id, r, g, b in entries:
            key = id(ctrl)
            if key not in groups:
                groups[key] = (ctrl, [])
            groups[key][1].append((zone_id, r, g, b))

        for ctrl, zones in groups.values():
            try:
                ctrl.set_mode(mode, speed=int(self._speed))
                for zone_id, r, g, b in zones:
                    ctrl.set_color(r, g, b, zone=zone_id)
                ctrl.apply()
            except (IOError, OSError, TimeoutError):
                pass

    def _run_loop(self) -> None:
        frame_time = 1.0 / 30.0
        t = 0.0

        while not self._stop_event.is_set():
            start = time.monotonic()

            # Build animated entries
            animated = []
            for ctrl, zone_id, r, g, b in self._entries:
                ar, ag, ab = self._compute_frame(t, (r, g, b))
                animated.append((ctrl, zone_id, ar, ag, ab))

            # Apply all
            groups: dict[int, tuple[RGBController, list[tuple]]] = {}
            for ctrl, zone_id, r, g, b in animated:
                key = id(ctrl)
                if key not in groups:
                    groups[key] = (ctrl, [])
                groups[key][1].append((zone_id, r, g, b))

            for ctrl, zones in groups.values():
                try:
                    for zone_id, r, g, b in zones:
                        ctrl.set_color(r, g, b, zone=zone_id)
                    ctrl.apply()
                except (IOError, OSError, TimeoutError):
                    pass

            t += frame_time * self._speed
            elapsed = time.monotonic() - start
            sleep_time = frame_time - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def _compute_frame(self, t: float,
                       base: tuple[int, int, int]) -> tuple[int, int, int]:
        if self._effect == "breathing":
            return self._breathing(t, base)
        elif self._effect == "color_cycle":
            return self._color_cycle(t)
        elif self._effect == "rainbow":
            return self._rainbow(t)
        elif self._effect == "strobe":
            return self._strobe(t, base)
        else:
            return base

    def _breathing(self, t: float,
                   base: tuple[int, int, int]) -> tuple[int, int, int]:
        brightness = (math.sin(t * 2.0) + 1.0) / 2.0
        r, g, b = base
        return int(r * brightness), int(g * brightness), int(b * brightness)

    def _color_cycle(self, t: float) -> tuple[int, int, int]:
        hue = (t * 0.1) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        return int(r * 255), int(g * 255), int(b * 255)

    def _rainbow(self, t: float) -> tuple[int, int, int]:
        hue = (t * 0.05) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        return int(r * 255), int(g * 255), int(b * 255)

    def _strobe(self, t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
        on = int(t * 4.0) % 2 == 0
        return base if on else (0, 0, 0)
