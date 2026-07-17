import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigCliTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.update(env_overrides or {})
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "money"
        result = self._run_cli(["setup", "--root", str(root)], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def test_config_prints_active_config_in_human_and_json_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            expected = json.loads(config_path.read_text(encoding="utf-8"))

            human = self._run_cli(["config"], cwd=root)
            machine = self._run_cli(["config", "--json"], cwd=root)

            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertEqual(json.loads(human.stdout), expected)
            self.assertEqual(machine.returncode, 0, machine.stderr)
            payload = json.loads(machine.stdout)
            self.assertEqual(payload["command"], "config")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["config"], expected)
            self.assertEqual(
                payload["artifacts"]["config_json"], str(config_path.resolve())
            )

    def test_config_edit_ollama_accepts_model_and_enables_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"

            result = self._run_cli(
                [
                    "config",
                    "edit",
                    "ollama",
                    "--model",
                    "qwen3.5:4b",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(config["ollama"]["enabled"])
            self.assertEqual(config["ollama"]["model"], "qwen3.5:4b")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["ollama"], config["ollama"])

    def test_config_edit_ollama_interactively_selects_local_model(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                body = {
                    "models": [
                        {"name": "zeta:latest"},
                        {"name": "alpha:latest"},
                    ]
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"]["url"] = (
                f"http://127.0.0.1:{server.server_port}/api/generate"
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self._run_cli(
                ["config", "edit", "ollama"], cwd=root, input_text="2\n"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Available Ollama models:", result.stdout)
            self.assertIn("1. alpha:latest", result.stdout)
            self.assertIn("2. zeta:latest", result.stdout)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(updated["ollama"]["enabled"])
            self.assertEqual(updated["ollama"]["model"], "zeta:latest")
            self.assertEqual(requests, ["/api/tags"])

    def test_config_edit_ollama_can_disable_without_selecting_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"]["enabled"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self._run_cli(["config", "edit", "ollama", "--disable"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(updated["ollama"]["enabled"])

    def test_config_edit_ollama_validates_configured_model_before_enabling(
        self,
    ) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = {"models": [{"name": "available:latest"}]}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"].update(
                {
                    "enabled": False,
                    "model": "missing:model",
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self._run_cli(
                ["config", "edit", "ollama", "--enable", "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertIn("is not installed", payload["errors"][0]["message"])
            unchanged = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(unchanged["ollama"]["enabled"])

    def test_config_edit_uses_editor_and_commits_only_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            original_mode = config_path.stat().st_mode
            editor = Path(tmp) / "editor.py"
            editor.write_text(
                "import json, pathlib, sys\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "config = json.loads(path.read_text())\n"
                "config['review_confidence_threshold'] = 0.9\n"
                "path.write_text(json.dumps(config))\n",
                encoding="utf-8",
            )

            result = self._run_cli(
                ["config", "edit"],
                cwd=root,
                env_overrides={"EDITOR": f"{sys.executable} {editor}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["review_confidence_threshold"], 0.9)
            self.assertEqual(config_path.stat().st_mode, original_mode)

    def test_config_edit_rejects_invalid_json_without_changing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            original = config_path.read_bytes()
            editor = Path(tmp) / "editor.py"
            editor.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text('{not json')\n",
                encoding="utf-8",
            )

            result = self._run_cli(
                ["config", "edit"],
                cwd=root,
                env_overrides={"EDITOR": f"{sys.executable} {editor}"},
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("property name", result.stderr)
            self.assertEqual(config_path.read_bytes(), original)

    def test_config_rejects_invalid_category_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["category_policies"] = {
                "Income": {"kind": "spending", "description": "Not allowed"}
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self._run_cli(["config", "--json"], cwd=root)

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertIn("cannot relax", payload["errors"][0]["message"])

    def test_config_rejects_malformed_public_fields_with_structured_errors(
        self,
    ) -> None:
        cases = [
            ("profiles", {"profiles": "profile.json"}, "must be a JSON array"),
            ("profiles item", {"profiles": [3]}, "profiles[0]"),
            ("profile mappings", {"profile_mappings": []}, "non-empty string"),
            ("rules", {"rules": {}}, "non-empty string"),
            ("corrections", {"corrections": 4}, "non-empty string"),
            ("pdf", {"pdf": []}, "must be a JSON object"),
            ("pdf enabled", {"pdf": {"enabled": "yes"}}, "must be a boolean"),
            ("exchange rates", {"exchange_rates": []}, "must be a JSON object"),
            (
                "exchange rate value",
                {"exchange_rates": {"HKD": float("nan")}},
                "finite number greater than 0",
            ),
            ("categories", {"categories": "Dining"}, "must be a JSON array"),
            ("empty categories", {"categories": []}, "must not be empty"),
            ("categories item", {"categories": [""]}, "categories[0]"),
            ("empty owners", {"owners": []}, "must not be empty"),
            ("owners", {"owners": [False]}, "owners[0]"),
            (
                "empty payment methods",
                {"payment_methods": []},
                "must not be empty",
            ),
            (
                "payment methods",
                {"payment_methods": ["Cash", "Cash"]},
                "must not contain duplicates",
            ),
            ("base currency", {"base_currency": 1}, "non-empty string"),
            (
                "review threshold",
                {"review_confidence_threshold": True},
                "number from 0 to 1",
            ),
            (
                "ollama batch",
                {"ollama": {"batch_size": 0}},
                "positive integer",
            ),
            (
                "ollama timeout",
                {"ollama": {"timeout_seconds": float("inf")}},
                "finite number greater than 0",
            ),
            (
                "ollama remote endpoint",
                {"ollama": {"url": "http://192.0.2.10:11434/api/generate"}},
                "local loopback",
            ),
            (
                "ollama endpoint credentials",
                {"ollama": {"url": "http://user:secret@localhost:11434/api/generate"}},
                "must not include credentials",
            ),
            ("ollama think", {"ollama": {"think": 1}}, "boolean or string"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            for label, config, message in cases:
                with self.subTest(label=label):
                    config_path.write_text(json.dumps(config), encoding="utf-8")

                    result = self._run_cli(
                        ["config", "--config", str(config_path), "--json"], cwd=root
                    )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, "")
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["command"], "config")
                    self.assertEqual(payload["status"], "error")
                    self.assertIn(message, payload["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
