"""Driver-facing errors. Kept free of transport/runner concerns."""

from __future__ import annotations


class DriverError(Exception):
    """Base class for driver failures."""


class DriverUnavailable(DriverError):
    """The driver's backing CLI/account is not usable."""


class UnsupportedCapability(DriverError):
    """The request asks for something the driver does not declare."""


class ProtocolViolation(DriverError):
    """The backing CLI emitted a frame that violates the official protocol."""
