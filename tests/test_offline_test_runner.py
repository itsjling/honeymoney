import _socket
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_tests_offline


class OfflineTestRunnerTest(unittest.TestCase):
    def test_network_guard_is_active_during_test_discovery(self) -> None:
        discovery_calls = []

        def guarded_discover(start_dir: str) -> unittest.TestSuite:
            discovery_calls.append(start_dir)
            with self.assertRaisesRegex(AssertionError, "creating sockets"):
                socket.socket()
            with self.assertRaisesRegex(AssertionError, "creating sockets"):
                socket.create_connection(("localhost", 80))
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.getaddrinfo("example.test", 80)
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.gethostbyname("127.0.0.1")
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.gethostbyname_ex("127.0.0.1")
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.gethostbyaddr("127.0.0.1")
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.getnameinfo(
                    ("127.0.0.1", 80),
                    socket.NI_NUMERICHOST | socket.NI_NUMERICSERV,
                )
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                socket.getfqdn("127.0.0.1")
            with self.assertRaisesRegex(AssertionError, "DNS resolution"):
                _socket.gethostbyname("127.0.0.1")
            return unittest.TestSuite()

        with (
            patch.object(
                unittest.defaultTestLoader,
                "discover",
                side_effect=guarded_discover,
            ),
            patch.object(
                unittest.TextTestRunner,
                "run",
                return_value=unittest.TestResult(),
            ),
        ):
            result = run_tests_offline.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            discovery_calls,
            [str(run_tests_offline.REPO_ROOT / "tests")],
        )

    def test_network_guard_is_inherited_by_child_python_processes(self) -> None:
        child_result = None

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sitecustomize.py").write_text(
                '"""Unrelated child-process customization."""\n',
                encoding="utf-8",
            )
            mixed_pythonpath = os.pathsep.join(
                [
                    tmp,
                    str(run_tests_offline.REPO_ROOT / "tests" / "offline_ollama_hook"),
                ]
            )

            def guarded_discover(start_dir: str) -> unittest.TestSuite:
                nonlocal child_result
                child_result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import ssl; import socket; socket.socket()",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return unittest.TestSuite()

            with (
                patch.dict(os.environ, {"PYTHONPATH": mixed_pythonpath}),
                patch.object(
                    unittest.defaultTestLoader,
                    "discover",
                    side_effect=guarded_discover,
                ),
                patch.object(
                    unittest.TextTestRunner,
                    "run",
                    return_value=unittest.TestResult(),
                ),
            ):
                result = run_tests_offline.main()

        self.assertEqual(result, 0)
        assert child_result is not None
        self.assertNotEqual(child_result.returncode, 0)
        self.assertIn(
            "default tests must inject network transports",
            child_result.stderr,
        )

    def test_direct_dns_guard_is_inherited_by_default_and_composed_children(
        self,
    ) -> None:
        resolver_calls = {
            "gethostbyname": "socket.gethostbyname('127.0.0.1')",
            "gethostbyname_ex": "socket.gethostbyname_ex('127.0.0.1')",
            "gethostbyaddr": "socket.gethostbyaddr('127.0.0.1')",
            "getnameinfo": (
                "socket.getnameinfo(('127.0.0.1', 80), "
                "socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)"
            ),
            "getfqdn": "socket.getfqdn('127.0.0.1')",
            "_socket_gethostbyname": "_socket.gethostbyname('127.0.0.1')",
        }
        hook_root = run_tests_offline.REPO_ROOT / "tests"
        scenarios = {
            "default": ("", {}),
            "fault_hook": (str(hook_root / "fault_injection"), {}),
            "ollama_hook": (
                str(hook_root / "offline_ollama_hook"),
                {"HONEYMONEY_TEST_OLLAMA_MODE": "unavailable"},
            ),
        }

        for scenario, (pythonpath, extra_environment) in scenarios.items():
            with self.subTest(scenario=scenario):
                child_results: dict[str, subprocess.CompletedProcess[str]] = {}

                def guarded_discover(start_dir: str) -> unittest.TestSuite:
                    del start_dir
                    for name, expression in resolver_calls.items():
                        child_results[name] = subprocess.run(
                            [
                                sys.executable,
                                "-c",
                                f"import ssl; import _socket; import socket; {expression}",
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                    return unittest.TestSuite()

                environment = {
                    "PYTHONPATH": pythonpath,
                    **extra_environment,
                }
                with (
                    patch.dict(os.environ, environment),
                    patch.object(
                        unittest.defaultTestLoader,
                        "discover",
                        side_effect=guarded_discover,
                    ),
                    patch.object(
                        unittest.TextTestRunner,
                        "run",
                        return_value=unittest.TestResult(),
                    ),
                ):
                    result = run_tests_offline.main()

                self.assertEqual(result, 0)
                self.assertEqual(set(child_results), set(resolver_calls))
                for name, child_result in child_results.items():
                    with self.subTest(scenario=scenario, resolver=name):
                        self.assertNotEqual(child_result.returncode, 0)
                        self.assertIn(
                            "must not perform direct DNS resolution",
                            child_result.stderr,
                        )


if __name__ == "__main__":
    unittest.main()
