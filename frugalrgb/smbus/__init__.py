import sys

from .interface import SMBusInterface


def get_smbus(bus_number: int | None = None) -> SMBusInterface:
    """Return the appropriate SMBus backend for the current platform.

    bus_number=None auto-detects the motherboard SMBus adapter on Linux
    and means bus 0 on Windows.
    """
    if sys.platform == "linux":
        from .linux import LinuxSMBus
        return LinuxSMBus(bus_number)
    elif sys.platform == "win32":
        from .windows import WindowsSMBus
        return WindowsSMBus(bus_number if bus_number is not None else 0)
    else:
        raise OSError(f"Unsupported platform: {sys.platform}")
