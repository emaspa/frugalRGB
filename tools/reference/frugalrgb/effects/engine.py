import colorsys
import logging
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


log = logging.getLogger(__name__)

# One zone's target state: (controller, zone_id, colour). The colour is an
# (r, g, b) tuple, or None when the zone must be dark.
Entry = tuple[RGBController, int | None, tuple[int, int, int] | None]


def resolve_entries(cards) -> list[Entry]:
    """Turn the UI's zone cards into the state to write to each device.

    cards: (controller, zone_id, enabled, (r, g, b)) in display order.

    A checked zone is lit with its colour and an unchecked zone is driven
    dark, so what the cards show is what the hardware gets. A device with no
    checked zone at all is left untouched. An "All Zones" card is written
    first and covers LEDs that have no card of their own; every other zone
    then gets its own state on top of it.
    """
    groups: dict[int, tuple[RGBController, list[tuple]]] = {}
    for ctrl, zone_id, enabled, color in cards:
        groups.setdefault(id(ctrl), (ctrl, []))[1].append(
            (zone_id, enabled, tuple(color))
        )

    entries: list[Entry] = []
    for ctrl, zones in groups.values():
        if not any(enabled for _zid, enabled, _color in zones):
            continue
        all_ids = {z.zone_id for z in ctrl.zones if z.is_all}
        # Stable sort: the all-zones write goes first, the rest keep their order
        for zone_id, enabled, color in sorted(
            zones, key=lambda z: z[0] not in all_ids
        ):
            entries.append((ctrl, zone_id, color if enabled else None))
    return entries


def _group_by_controller(
    entries: list[Entry],
) -> list[tuple[RGBController, list[tuple]]]:
    groups: dict[int, tuple[RGBController, list[tuple]]] = {}
    for ctrl, zone_id, color in entries:
        groups.setdefault(id(ctrl), (ctrl, []))[1].append((zone_id, color))
    return list(groups.values())


class EffectEngine:
    """Runs RGB effects — uses hardware modes when available, software fallback otherwise.

    Entries are (controller, zone_id, colour) tuples, see ``Entry``. Multiple
    entries can reference the same controller with different zones/colours.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._controllers: list[RGBController] = []
        self._effect: str = "static"
        self._speed: float = 1.0
        self._entries: list[Entry] = []

    def set_controllers(self, controllers: list[RGBController]) -> None:
        self._controllers = controllers

    def start_effect(self, effect: str, speed: float,
                     entries: list[Entry]) -> None:
        """Start an effect.

        entries: list of (ctrl, zone_id, colour) tuples, see ``Entry``.
        """
        self.stop()
        self._effect = effect
        self._speed = speed

        mode = EFFECT_MODE_MAP.get(effect, RGBMode.STATIC)

        # Apply once: group by controller for efficient set_mode/apply
        self._apply_entries(mode, entries)

        if effect in ("static", "off"):
            self._entries = []
            return

        # Only devices that can't animate this mode themselves need the
        # software loop. Streaming frames to a device that is running the
        # effect in hardware would fight its own animation.
        self._entries = [
            e for e in entries if not getattr(e[0], "has_hardware_mode", False)
        ]
        if not self._entries:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=1.0)
            self._thread = None

    def turn_off(self, entries: list[Entry]) -> None:
        self.stop()
        mode = RGBMode.OFF
        off_entries = [(ctrl, zid, (0, 0, 0)) for ctrl, zid, *_ in entries]
        self._apply_entries(mode, off_entries)

    @staticmethod
    def _write_zones(ctrl: RGBController, zones: list[tuple]) -> None:
        for zone_id, color in zones:
            if color is None:
                ctrl.set_zone_off(zone_id)
            else:
                ctrl.set_color(*color, zone=zone_id)
        ctrl.apply()

    def _apply_entries(self, mode: RGBMode,
                       entries: list[Entry]) -> None:
        """Group entries by controller, set mode once, colors per zone, apply once."""
        for ctrl, zones in _group_by_controller(entries):
            try:
                ctrl.set_mode(mode, speed=int(self._speed))
                self._write_zones(ctrl, zones)
            except (IOError, OSError, TimeoutError) as e:
                log.warning("Failed to apply %s on %s: %s",
                            mode.name, ctrl.name, e)

    def _run_loop(self) -> None:
        frame_time = 1.0 / 30.0
        t = 0.0

        while not self._stop_event.is_set():
            start = time.monotonic()

            # Animate lit zones; dark zones stay dark
            animated = [
                (ctrl, zone_id,
                 None if color is None else self._compute_frame(t, color))
                for ctrl, zone_id, color in self._entries
            ]

            for ctrl, zones in _group_by_controller(animated):
                try:
                    self._write_zones(ctrl, zones)
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
