from .interface import SMBusInterface


class LinuxSMBus(SMBusInterface):
    """Linux SMBus backend using smbus2 (/dev/i2c-*)."""

    def __init__(self, bus_number: int = 0):
        self._bus_number = bus_number
        self._bus = None

    def open(self) -> None:
        from smbus2 import SMBus
        self._bus = SMBus(self._bus_number)

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
