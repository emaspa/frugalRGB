from abc import ABC, abstractmethod
from enum import IntEnum


class RGBMode(IntEnum):
    OFF = 0
    STATIC = 1
    BREATHING = 2
    COLOR_CYCLE = 3
    RAINBOW = 4
    STROBE = 5


class RGBZone:
    """Represents a single controllable RGB zone on a device."""

    def __init__(self, zone_id: int, name: str, is_all: bool = False):
        self.zone_id = zone_id
        self.name = name
        # True for a pseudo-zone that addresses every LED on the device
        # ("All Zones"), as opposed to one physical zone.
        self.is_all = is_all


class RGBController(ABC):
    """Base class for all RGB device controllers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable device name."""

    @property
    @abstractmethod
    def zones(self) -> list[RGBZone]:
        """List of controllable zones."""

    @property
    @abstractmethod
    def supported_modes(self) -> list[RGBMode]:
        """List of modes this device supports."""

    @abstractmethod
    def set_color(self, r: int, g: int, b: int, zone: int | None = None) -> None:
        """Set a static color. If zone is None, set all zones."""

    def set_zone_off(self, zone: int | None = None) -> None:
        """Make one zone dark while the rest of the device keeps its mode.

        The default writes black in the current mode, which is dark for every
        mode that honours the zone colour. Controllers with a per-zone mode
        override this so the zone also stays dark in colour-cycling modes.
        """
        self.set_color(0, 0, 0, zone=zone)

    @abstractmethod
    def set_mode(self, mode: RGBMode, speed: int = 3) -> None:
        """Set the lighting mode. Speed 0 (fastest) to 5 (slowest)."""

    @abstractmethod
    def apply(self) -> None:
        """Commit pending changes to hardware."""

    @property
    def supports_hardware_save(self) -> bool:
        """Whether this device supports saving settings to non-volatile memory."""
        return False

    def save_to_hardware(self) -> None:
        """Save current settings to non-volatile memory (survives power cycles)."""
        raise NotImplementedError
