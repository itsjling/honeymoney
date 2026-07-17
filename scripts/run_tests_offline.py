#!/usr/bin/env python3
"""Run default tests while forbidding in-process network access."""

from __future__ import annotations

import ipaddress
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _forbid_socket(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError(
        "default tests must inject network transports instead of creating sockets"
    )


def _offline_getaddrinfo(
    host: str,
    port: int,
    *,
    family: int = socket.AF_UNSPEC,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple]:
    del family, proto, flags
    normalized = host.casefold()
    if normalized == "localhost":
        addresses = ["127.0.0.1", "::1"]
    else:
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError as error:
            raise AssertionError(
                f"default tests attempted DNS resolution for {host!r}"
            ) from error
    results = []
    for address in addresses:
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        socket_address = (
            (address, port, 0, 0)
            if address_family == socket.AF_INET6
            else (address, port)
        )
        results.append(
            (
                address_family,
                type or socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                socket_address,
            )
        )
    return results


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"))
    with (
        patch("socket.socket", side_effect=_forbid_socket),
        patch("socket.create_connection", side_effect=_forbid_socket),
        patch("socket.getaddrinfo", side_effect=_offline_getaddrinfo),
    ):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
