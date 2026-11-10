from abc import ABC, abstractmethod


class SMBusInterface(ABC):
    """Abstract SMBus interface — platform backends implement this."""

    @abstractmethod
    def open(self) -> None:
        """Open the SMBus connection."""

    @abstractmethod
    def close(self) -> None:
        """Close the SMBus connection."""

    @abstractmethod
    def read_byte_data(self, addr: int, cmd: int) -> int:
        """Read a single byte from device `addr` at register `cmd`."""

    @abstractmethod
    def write_byte_data(self, addr: int, cmd: int, value: int) -> None:
        """Write a single byte `value` to device `addr` at register `cmd`."""

    @abstractmethod
    def write_word_data(self, addr: int, cmd: int, value: int) -> None:
        """Write a 16-bit word `value` to device `addr` at register `cmd`."""

    @abstractmethod
    def write_block_data(self, addr: int, cmd: int, data: list[int]) -> None:
        """Write a block of bytes to device `addr` at register `cmd`."""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
