"""Network guard shared by the default test process and child Python processes."""

from __future__ import annotations

import _socket
import ipaddress
import socket
from typing import NoReturn

SocketAddress = tuple[str, int] | tuple[str, int, int, int]
AddressInfo = tuple[int, int, int, str, SocketAddress]


def forbid_socket(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError(
        "default tests must inject network transports instead of creating sockets"
    )


def forbid_dns(*args: object, **kwargs: object) -> NoReturn:
    del args, kwargs
    raise AssertionError("default tests must not perform direct DNS resolution")


def offline_getaddrinfo(
    host: str,
    port: int,
    *,
    family: int = socket.AF_UNSPEC,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[AddressInfo]:
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
    results: list[AddressInfo] = []
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


def install() -> None:
    setattr(socket, "socket", forbid_socket)
    setattr(socket, "SocketType", forbid_socket)
    setattr(_socket, "socket", forbid_socket)
    setattr(_socket, "SocketType", forbid_socket)
    setattr(socket, "create_connection", forbid_socket)
    setattr(socket, "getaddrinfo", offline_getaddrinfo)
    setattr(socket, "gethostbyname", forbid_dns)
    setattr(socket, "gethostbyname_ex", forbid_dns)
    setattr(socket, "gethostbyaddr", forbid_dns)
    setattr(socket, "getnameinfo", forbid_dns)
    setattr(socket, "getfqdn", forbid_dns)
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        setattr(_socket, name, forbid_dns)
