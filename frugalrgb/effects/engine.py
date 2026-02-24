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
    """Runs RGB effects — uses hardware modes when available, software fallback otherwise."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._controllers: list[RGBController] = []
        self._effect: str = "static"
        self._color: tuple[int, int, int] = (255, 255, 255)
        self._speed: float = 1.0
        self._zone_map: dict = {}
        self._disabled: set = set()
        self._color_map: dict[int, tuple[int, int, int]] = {}

    def set_controllers(self, controllers: list[RGBController]) -> None:
        self._controllers = controllers

    def _active_controllers(self) -> list[RGBController]:
        return [c for c in self._controllers if id(c) not in self._disabled]

    def start_effect(self, effect: str, r: int, g: int, b: int, speed: float = 1.0,
                     zone_map: dict | None = None, disabled: set | None = None,
                     color_map: dict | None = None) -> None:
        self.stop()
        self._color = (r, g, b)
        self._speed = speed
        self._effect = effect
        self._zone_map = zone_map or {}
        self._disabled = disabled or set()
        self._color_map = color_map or {}

        mode = EFFECT_MODE_MAP.get(effect, RGBMode.STATIC)
        active = self._active_controllers()

        for ctrl in active:
            zone_id = self._zone_map.get(id(ctrl))
            cr, cg, cb = self._color_map.get(id(ctrl), (r, g, b))
            ctrl.set_mode(mode, speed=int(speed))
            ctrl.set_color(cr, cg, cb, zone=zone_id)
            ctrl.apply()

        use_hardware = all(
            getattr(ctrl, "has_hardware_mode", False) for ctrl in active
        ) if active else True

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

    def turn_off(self, zone_map: dict | None = None, disabled: set | None = None) -> None:
        self.stop()
        zone_map = zone_map or {}
        self._disabled = disabled or set()
        for ctrl in self._active_controllers():
            zone_id = zone_map.get(id(ctrl))
            ctrl.set_mode(RGBMode.OFF)
            ctrl.set_color(0, 0, 0, zone=zone_id)
            ctrl.apply()

    def _run_loop(self) -> None:
        frame_time = 1.0 / 30.0
        t = 0.0

        while not self._stop_event.is_set():
            start = time.monotonic()

            for ctrl in self._active_controllers():
                try:
                    base = self._color_map.get(id(ctrl), self._color)
                    r, g, b = self._compute_frame(t, base)
                    zone_id = self._zone_map.get(id(ctrl))
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
