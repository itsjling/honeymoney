import socket
import unittest

from honeymoney.ollama import (
    LoopbackOllamaTransport,
    OllamaHttpRequest,
    OllamaHttpResponse,
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
                self.assertEqual(sent[0].timeout, 3.5)

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
