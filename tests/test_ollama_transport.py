import errno
import socket
import threading
import unittest
import urllib.error
from unittest.mock import patch

from honeymoney.ollama import (
    LoopbackOllamaTransport,
    OllamaHttpRequest,
    OllamaHttpResponse,
    _default_sender,
    _OllamaConnectFailure,
    list_ollama_models,
)


def resolved(*addresses: str):
    def resolver(host: str, port: int, **kwargs: object) -> list[tuple]:
        del host, kwargs
        results = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (
                (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            )
            results.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return results

    return resolver


class OllamaTransportTest(unittest.TestCase):
    def test_default_sender_preserves_pinned_request_and_response(self) -> None:
        connections = []

        class Response:
            status = 201
            reason = "Created"

            def getheaders(self) -> list[tuple[str, str]]:
                return [("Content-Type", "application/json"), ("X-Synthetic", "yes")]

            def read(self) -> bytes:
                return b'{"synthetic": "response"}'

        class Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                self.init = (host, port, timeout)
                self.request_args = ()
                self.closed = False
                connections.append(self)

            def connect(self) -> None:
                return None

            def request(self, *args: object, **kwargs: object) -> None:
                self.request_args = (args, kwargs)

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                self.closed = True

        request = OllamaHttpRequest(
            "POST",
            "http://[::1]:11434/api/generate?format=json",
            {"Host": "localhost:11434", "Content-Type": "application/json"},
            b'{"synthetic": "request"}',
            3.5,
        )
        with patch("honeymoney.ollama.http.client.HTTPConnection", Connection):
            response = _default_sender(request)

        [connection] = connections
        self.assertEqual(connection.init, ("::1", 11434, 3.5))
        self.assertEqual(
            connection.request_args,
            (
                ("POST", "/api/generate?format=json"),
                {
                    "body": b'{"synthetic": "request"}',
                    "headers": {
                        "Host": "localhost:11434",
                        "Content-Type": "application/json",
                    },
                },
            ),
        )
        self.assertTrue(connection.closed)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.reason, "Created")
        self.assertEqual(response.headers["X-Synthetic"], "yes")
        self.assertEqual(response.body, b'{"synthetic": "response"}')

    def test_default_sender_marks_only_connect_phase_failures(self) -> None:
        class Connection:
            failure_phase = "connect"
            failure: Exception = TimeoutError("synthetic connect timeout")

            def __init__(self, host: str, port: int, timeout: float) -> None:
                del host, port, timeout

            def connect(self) -> None:
                if self.failure_phase == "connect":
                    raise self.failure

            def request(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def getresponse(self) -> object:
                if self.failure_phase == "headers":
                    raise self.failure

                class Response:
                    status = 200
                    reason = "OK"

                    def getheaders(self) -> list[tuple[str, str]]:
                        return []

                    def read(inner_self) -> bytes:
                        del inner_self
                        raise self.failure

                return Response()

            def close(self) -> None:
                return None

        request = OllamaHttpRequest(
            "POST",
            "http://127.0.0.1:11434/api/generate",
            {"Host": "localhost:11434"},
            b'{"synthetic": "payload"}',
            1.0,
        )
        with patch("honeymoney.ollama.http.client.HTTPConnection", Connection):
            with self.assertRaises(_OllamaConnectFailure):
                _default_sender(request)

            for phase, failure in (
                ("headers", TimeoutError("synthetic response header timeout")),
                ("body", ConnectionResetError("synthetic response body reset")),
            ):
                with self.subTest(phase=phase):
                    Connection.failure_phase = phase
                    Connection.failure = failure
                    with self.assertRaises(type(failure)):
                        _default_sender(request)

    def test_default_sender_rejects_response_headers_after_deadline(self) -> None:
        elapsed = 0.0

        def monotonic() -> float:
            return 100.0 + elapsed

        class Socket:
            def settimeout(self, timeout: float) -> None:
                del timeout

            def shutdown(self, how: int) -> None:
                del how

            def close(self) -> None:
                return None

        class Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                del host, port, timeout
                self.sock = Socket()

            def connect(self) -> None:
                return None

            def request(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def getresponse(self) -> object:
                nonlocal elapsed
                elapsed = 1.1

                class Response:
                    status = 200
                    reason = "OK"

                    def getheaders(self) -> list[tuple[str, str]]:
                        return []

                    def read(self) -> bytes:
                        return b'{"synthetic": "headers"}'

                return Response()

            def close(self) -> None:
                return None

        request = OllamaHttpRequest(
            "GET",
            "http://127.0.0.1:11434/api/tags",
            {"Host": "localhost:11434"},
            None,
            1.0,
        )
        with (
            patch("honeymoney.ollama.http.client.HTTPConnection", Connection),
            patch("honeymoney.ollama.time.monotonic", monotonic),
        ):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                _default_sender(request)

    def test_default_sender_rejects_trickled_body_after_deadline(self) -> None:
        elapsed = 0.0

        def monotonic() -> float:
            return 100.0 + elapsed

        class Socket:
            def settimeout(self, timeout: float) -> None:
                del timeout

            def shutdown(self, how: int) -> None:
                del how

            def close(self) -> None:
                return None

        class Response:
            status = 200
            reason = "OK"

            def getheaders(self) -> list[tuple[str, str]]:
                return []

            def read(self) -> bytes:
                nonlocal elapsed
                elapsed = 1.1
                return b'{"synthetic": "trickled"}'

        class Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                del host, port, timeout
                self.sock = Socket()

            def connect(self) -> None:
                return None

            def request(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def getresponse(self) -> Response:
                nonlocal elapsed
                elapsed = 0.4
                return Response()

            def close(self) -> None:
                return None

        request = OllamaHttpRequest(
            "GET",
            "http://127.0.0.1:11434/api/tags",
            {"Host": "localhost:11434"},
            None,
            1.0,
        )
        with (
            patch("honeymoney.ollama.http.client.HTTPConnection", Connection),
            patch("honeymoney.ollama.time.monotonic", monotonic),
        ):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                _default_sender(request)

    def test_default_sender_interrupts_a_stalled_body_at_deadline(self) -> None:
        closed = threading.Event()
        test_case = self

        class Socket:
            def settimeout(self, timeout: float) -> None:
                del timeout

            def shutdown(self, how: int) -> None:
                del how

            def close(self) -> None:
                closed.set()

        class Response:
            status = 200
            reason = "OK"

            def getheaders(self) -> list[tuple[str, str]]:
                return []

            def read(self) -> bytes:
                test_case.assertTrue(closed.wait(0.5))
                return b'{"synthetic": "stalled"}'

        class Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                del host, port, timeout
                self.sock = Socket()

            def connect(self) -> None:
                return None

            def request(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                return None

        request = OllamaHttpRequest(
            "GET",
            "http://127.0.0.1:11434/api/tags",
            {"Host": "localhost:11434"},
            None,
            0.05,
        )
        with patch("honeymoney.ollama.http.client.HTTPConnection", Connection):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                _default_sender(request)

    def test_loopback_hosts_are_pinned_before_sending(self) -> None:
        cases = [
            ("localhost", resolved("127.0.0.1", "::1"), "127.0.0.1"),
            ("127.0.0.1", resolved("127.0.0.1"), "127.0.0.1"),
            ("[::1]", resolved("::1"), "[::1]"),
        ]
        for host, resolver, pinned_host in cases:
            with self.subTest(host=host):
                sent = []
                transport = LoopbackOllamaTransport(
                    resolver=resolver,
                    sender=lambda request: (
                        sent.append(request) or OllamaHttpResponse(200, "OK", {}, b"{}")
                    ),
                )

                body = transport.request(
                    OllamaHttpRequest(
                        "POST",
                        f"http://{host}:11434/api/generate",
                        {"Content-Type": "application/json"},
                        b"{}",
                        3.5,
                    )
                )

                self.assertEqual(body, b"{}")
                self.assertEqual(
                    sent[0].url,
                    f"http://{pinned_host}:11434/api/generate",
                )
                self.assertEqual(sent[0].headers["Host"], f"{host}:11434")
                self.assertGreater(sent[0].timeout, 0)
                self.assertLessEqual(sent[0].timeout, 3.5)

    def test_retries_the_next_validated_address_after_connection_refused(self) -> None:
        sent = []

        def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
            sent.append(request)
            if request.url.startswith("http://[::1]"):
                raise _OllamaConnectFailure(
                    ConnectionRefusedError("synthetic IPv6 listener is absent")
                )
            return OllamaHttpResponse(200, "OK", {}, b"{}")

        body = LoopbackOllamaTransport(
            resolver=resolved("::1", "127.0.0.1"), sender=sender
        ).request(
            OllamaHttpRequest(
                "POST",
                "http://localhost:11434/api/generate",
                {"Content-Type": "application/json"},
                b"{}",
                1.0,
            )
        )

        self.assertEqual(body, b"{}")
        self.assertEqual(
            [request.url for request in sent],
            [
                "http://[::1]:11434/api/generate",
                "http://127.0.0.1:11434/api/generate",
            ],
        )
        self.assertTrue(
            all(request.headers["Host"] == "localhost:11434" for request in sent)
        )

    def test_retries_after_timeout_and_unreachable_connection_failures(self) -> None:
        cases = [
            TimeoutError("synthetic connection timed out"),
            OSError(errno.EHOSTUNREACH, "synthetic host is unreachable"),
        ]
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                sent = []

                def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
                    sent.append(request)
                    if request.url.startswith("http://[::1]"):
                        raise _OllamaConnectFailure(failure)
                    return OllamaHttpResponse(200, "OK", {}, b"{}")

                body = LoopbackOllamaTransport(
                    resolver=resolved("::1", "127.0.0.1"), sender=sender
                ).request(
                    OllamaHttpRequest(
                        "GET",
                        "http://localhost:11434/api/tags",
                        {},
                        None,
                        1.0,
                    )
                )

                self.assertEqual(body, b"{}")
                self.assertEqual(
                    [request.url for request in sent],
                    [
                        "http://[::1]:11434/api/tags",
                        "http://127.0.0.1:11434/api/tags",
                    ],
                )

    def test_connection_retries_share_one_timeout_budget(self) -> None:
        elapsed = 0.0
        sent = []

        def monotonic() -> float:
            return 100.0 + elapsed

        def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
            nonlocal elapsed
            sent.append(request)
            elapsed += 2.0 if len(sent) == 1 else 3.0
            raise _OllamaConnectFailure(TimeoutError("synthetic connect timeout"))

        transport = LoopbackOllamaTransport(
            resolver=resolved("::1", "127.0.0.1", "127.0.0.2"), sender=sender
        )
        with patch("honeymoney.ollama.time.monotonic", monotonic):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                transport.request(
                    OllamaHttpRequest(
                        "GET",
                        "http://localhost:11434/api/tags",
                        {},
                        None,
                        5.0,
                    )
                )

        self.assertEqual(elapsed, 5.0)
        self.assertEqual([request.timeout for request in sent], [5.0, 3.0])
        self.assertEqual(
            [request.url for request in sent],
            [
                "http://[::1]:11434/api/tags",
                "http://127.0.0.1:11434/api/tags",
            ],
        )

    def test_model_listing_retries_the_next_validated_address(self) -> None:
        sent = []

        def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
            sent.append(request)
            if request.url.startswith("http://[::1]"):
                raise _OllamaConnectFailure(
                    ConnectionRefusedError("synthetic IPv6 listener is absent")
                )
            return OllamaHttpResponse(
                200, "OK", {}, b'{"models": [{"name": "synthetic:latest"}]}'
            )

        transport = LoopbackOllamaTransport(
            resolver=resolved("::1", "127.0.0.1"), sender=sender
        )

        self.assertEqual(
            list_ollama_models(
                {"url": "http://localhost:11434/api/generate"}, transport=transport
            ),
            ["synthetic:latest"],
        )
        self.assertEqual(
            [request.url for request in sent],
            [
                "http://[::1]:11434/api/tags",
                "http://127.0.0.1:11434/api/tags",
            ],
        )

    def test_response_timeout_and_reset_do_not_retry_generation(self) -> None:
        failures = [
            TimeoutError("synthetic response timed out"),
            ConnectionResetError("synthetic response was reset"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                sent = []

                def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
                    sent.append(request)
                    raise failure

                transport = LoopbackOllamaTransport(
                    resolver=resolved("::1", "127.0.0.1"), sender=sender
                )

                with self.assertRaises(type(failure)):
                    transport.request(
                        OllamaHttpRequest(
                            "POST",
                            "http://localhost:11434/api/generate",
                            {"Content-Type": "application/json"},
                            b'{"synthetic": "payload"}',
                            1.0,
                        )
                    )

                self.assertEqual(
                    [request.url for request in sent],
                    ["http://[::1]:11434/api/generate"],
                )

    def test_first_working_address_is_used_once_for_generation_and_model_listing(
        self,
    ) -> None:
        sent = []

        def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
            sent.append(request)
            if request.url.endswith("/api/tags"):
                return OllamaHttpResponse(
                    200, "OK", {}, b'{"models": [{"name": "synthetic:latest"}]}'
                )
            return OllamaHttpResponse(200, "OK", {}, b"{}")

        transport = LoopbackOllamaTransport(
            resolver=resolved("127.0.0.1", "::1"), sender=sender
        )
        self.assertEqual(
            list_ollama_models(
                {"url": "http://localhost:11434/api/generate"}, transport=transport
            ),
            ["synthetic:latest"],
        )
        self.assertEqual(
            transport.request(
                OllamaHttpRequest(
                    "POST",
                    "http://localhost:11434/api/generate",
                    {"Content-Type": "application/json"},
                    b"{}",
                    1.0,
                )
            ),
            b"{}",
        )
        self.assertEqual(
            [request.url for request in sent],
            [
                "http://127.0.0.1:11434/api/tags",
                "http://127.0.0.1:11434/api/generate",
            ],
        )

    def test_ipv6_only_server_is_used(self) -> None:
        sent = []
        transport = LoopbackOllamaTransport(
            resolver=resolved("::1"),
            sender=lambda request: (
                sent.append(request) or OllamaHttpResponse(200, "OK", {}, b"{}")
            ),
        )

        self.assertEqual(
            transport.request(
                OllamaHttpRequest(
                    "GET", "http://localhost:11434/api/tags", {}, None, 1.0
                )
            ),
            b"{}",
        )
        self.assertEqual(sent[0].url, "http://[::1]:11434/api/tags")

    def test_http_errors_do_not_try_another_validated_address(self) -> None:
        sent = []
        transport = LoopbackOllamaTransport(
            resolver=resolved("::1", "127.0.0.1"),
            sender=lambda request: (
                sent.append(request)
                or OllamaHttpResponse(503, "Service Unavailable", {}, b"synthetic")
            ),
        )

        with self.assertRaisesRegex(urllib.error.HTTPError, "503"):
            transport.request(
                OllamaHttpRequest(
                    "GET", "http://localhost:11434/api/tags", {}, None, 1.0
                )
            )

        self.assertEqual(
            [request.url for request in sent], ["http://[::1]:11434/api/tags"]
        )

    def test_non_loopback_and_malformed_urls_fail_before_sending(self) -> None:
        cases = [
            ("http://192.0.2.10:11434/api/generate", resolved("192.0.2.10")),
            ("http://ollama.example:11434/api/generate", resolved("192.0.2.10")),
            (
                "http://ollama.local:11434/api/generate",
                resolved("127.0.0.1", "192.0.2.10"),
            ),
            ("https://localhost:11434/api/generate", resolved("127.0.0.1")),
            ("file:///api/generate", resolved("127.0.0.1")),
            ("http://user:secret@localhost:11434/api/generate", resolved("127.0.0.1")),
            ("http://localhost:invalid/api/generate", resolved("127.0.0.1")),
            ("http://localhost:0/api/generate", resolved("127.0.0.1")),
            ("http://[::1", resolved("::1")),
        ]
        for url, resolver in cases:
            with self.subTest(url=url):
                sent = []
                transport = LoopbackOllamaTransport(
                    resolver=resolver,
                    sender=lambda request: (
                        sent.append(request) or OllamaHttpResponse(200, "OK", {}, b"{}")
                    ),
                )

                with self.assertRaisesRegex(ValueError, "Ollama endpoint"):
                    transport.request(OllamaHttpRequest("GET", url, {}, None, 1.0))

                self.assertEqual(sent, [])

    def test_redirect_to_non_loopback_is_rejected_before_following(self) -> None:
        sent = []

        def sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
            sent.append(request)
            return OllamaHttpResponse(
                302,
                "Found",
                {"Location": "http://192.0.2.10:11434/collect"},
                b"",
            )

        def resolver(host: str, port: int, **kwargs: object) -> list[tuple]:
            del kwargs
            address = "127.0.0.1" if host == "localhost" else "192.0.2.10"
            return resolved(address)(host, port)

        transport = LoopbackOllamaTransport(resolver=resolver, sender=sender)

        with self.assertRaisesRegex(ValueError, "loopback"):
            transport.request(
                OllamaHttpRequest(
                    "POST",
                    "http://localhost:11434/api/generate",
                    {"Content-Type": "application/json"},
                    b"{}",
                    1.0,
                )
            )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].url, "http://127.0.0.1:11434/api/generate")


if __name__ == "__main__":
    unittest.main()
