import socket
import unittest
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


if __name__ == "__main__":
    unittest.main()
