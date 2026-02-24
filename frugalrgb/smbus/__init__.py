import sys

from .interface import SMBusInterface


def get_smbus(bus_number: int = 0) -> SMBusInterface:
    """Return the appropriate SMBus backend for the current platform."""
    if sys.platform == "linux":
        from .linux import LinuxSMBus
        return LinuxSMBus(bus_number)
    elif sys.platform == "win32":
        from .windows import WindowsSMBus
        return WindowsSMBus(bus_number)
    else:
        raise OSError(f"Unsupported platform: {sys.platform}")
