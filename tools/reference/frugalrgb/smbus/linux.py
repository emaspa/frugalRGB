import glob
import logging

from .interface import SMBusInterface

log = logging.getLogger(__name__)

# Adapter name prefixes that carry the DIMM SPD/RGB bus, in preference order.
# Desktop systems expose many i2c adapters (GPU DDC, DesignWare, ...) — the
# motherboard SMBus is the one the chipset driver registers. On AMD the DIMMs
# live on PIIX4 port 0; port 2 (ASF) comes second as a fallback.
_ADAPTER_PREFERENCE = (
    "SMBus PIIX4 adapter port 0",  # AMD FCH, main port
    "SMBus PIIX4 adapter",         # AMD FCH, any port
    "SMBus I801 adapter",          # Intel PCH
    "SMBus",                       # anything else calling itself SMBus
)


class LinuxSMBus(SMBusInterface):
    """Linux SMBus backend using smbus2 (/dev/i2c-*)."""

    def __init__(self, bus_number: int | None = None):
        self._bus_number = bus_number
        self._bus = None

    @staticmethod
    def _find_adapter() -> tuple[int, str] | None:
        adapters: list[tuple[int, str]] = []
        for path in glob.glob("/sys/bus/i2c/devices/i2c-*/name"):
            try:
                with open(path) as f:
                    name = f.read().strip()
                num = int(path.split("/i2c-")[-1].split("/")[0])
            except (OSError, ValueError):
                continue
            adapters.append((num, name))
        adapters.sort()
        for pref in _ADAPTER_PREFERENCE:
            for num, name in adapters:
                if name.startswith(pref):
                    return num, name
        return None

    def open(self) -> None:
        from smbus2 import SMBus
        num = self._bus_number
        if num is None:
            found = self._find_adapter()
            if found is None:
                raise RuntimeError(
                    "no SMBus adapter found — is the chipset SMBus kernel "
                    "module loaded (i2c-piix4 on AMD, i2c-i801 on Intel)?"
                )
            num, name = found
            log.info("Using SMBus adapter i2c-%d (%s)", num, name)
        self._bus = SMBus(num)

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None

    def read_byte_data(self, addr: int, cmd: int) -> int:
        return self._bus.read_byte_data(addr, cmd)

    def write_byte_data(self, addr: int, cmd: int, value: int) -> None:
        self._bus.write_byte_data(addr, cmd, value)

    def write_word_data(self, addr: int, cmd: int, value: int) -> None:
        self._bus.write_word_data(addr, cmd, value)

    def write_block_data(self, addr: int, cmd: int, data: list[int]) -> None:
        self._bus.write_block_data(addr, cmd, data)
