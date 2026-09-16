"""An I2C device at one address on one bus, through `/dev/i2c-N` and the standard library.

A register read is two transfers: write the register number, then read. BME280 and
ADS1115 both accept that. It spares the board a dependency for four system calls. Two
sources on the same device share one `Device` and its lock, so their transfers never
interleave.
"""

from __future__ import annotations

import fcntl
import os
from threading import Lock
from typing import Protocol

I2C_SLAVE = 0x0703


class BusError(RuntimeError):
    """The bus or the device did not answer as a working device would."""


class Bus(Protocol):
    def read(self, register: int, length: int) -> bytes: ...

    def write(self, register: int, data: bytes) -> None: ...

    def close(self) -> None: ...


class Device:
    def __init__(self, bus: int, address: int) -> None:
        self.bus, self.address = bus, address
        self.lock = Lock()
        self._fd: int | None = None

    def _open(self) -> int:
        if self._fd is None:
            path = f"/dev/i2c-{self.bus}"
            try:
                fd = os.open(path, os.O_RDWR)
            except FileNotFoundError as error:
                raise BusError(f"{path} does not exist; is I2C enabled?") from error
            except OSError as error:
                raise BusError(f"{path}: {error}") from error
            try:
                fcntl.ioctl(fd, I2C_SLAVE, self.address)
            except OSError as error:
                os.close(fd)
                raise BusError(f"address 0x{self.address:02x} on {path}: {error}") from error
            self._fd = fd
        return self._fd

    def read(self, register: int, length: int) -> bytes:
        try:
            fd = self._open()
            os.write(fd, bytes([register]))
            data = os.read(fd, length)
        except OSError as error:
            self.close()
            raise BusError(self._where(error)) from error
        if len(data) != length:
            raise BusError(f"{self._label}: {len(data)} bytes instead of {length}")
        return data

    def write(self, register: int, data: bytes) -> None:
        try:
            os.write(self._open(), bytes([register]) + data)
        except OSError as error:
            self.close()
            raise BusError(self._where(error)) from error

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    @property
    def _label(self) -> str:
        return f"i2c-{self.bus} 0x{self.address:02x}"

    def _where(self, error: OSError) -> str:
        return f"{self._label} did not answer: {error.strerror or error}"
