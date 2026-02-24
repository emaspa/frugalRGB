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

    def __init__(self, zone_id: int, name: str):
        self.zone_id = zone_id
        self.name = name


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
