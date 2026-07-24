#!/usr/bin/env python3
"""Run default tests while forbidding in-process network access."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from scripts.offline_network_guard import (
        forbid_dns,
        forbid_socket,
        offline_getaddrinfo,
    )
except ModuleNotFoundError:
    from offline_network_guard import forbid_dns, forbid_socket, offline_getaddrinfo

REPO_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_GUARD_DIR = REPO_ROOT / "scripts" / "offline_guard"
OFFLINE_ENVIRONMENT_FLAG = "HONEYMONEY_TEST_OFFLINE"
OFFLINE_COMPOSING_HOOK_DIRS = frozenset(
    {
        REPO_ROOT / "tests" / "fault_injection",
        REPO_ROOT / "tests" / "offline_ollama_hook",
    }
)
sys.path.insert(0, str(REPO_ROOT))


def _child_environment(
    environment: dict[str, str] | None,
) -> dict[str, str]:
    child_environment = dict(os.environ if environment is None else environment)
    existing_paths = [
        item
        for item in child_environment.get("PYTHONPATH", "").split(os.pathsep)
        if item
    ]
    effective_sitecustomize_dir = next(
        (
            Path(item).resolve()
            for item in existing_paths
            if (Path(item) / "sitecustomize.py").is_file()
        ),
        None,
    )
    has_composing_sitecustomize = (
        effective_sitecustomize_dir in OFFLINE_COMPOSING_HOOK_DIRS
    )
    required_paths = [str(REPO_ROOT)]
    if not has_composing_sitecustomize:
        required_paths.insert(0, str(OFFLINE_GUARD_DIR))
    child_environment["PYTHONPATH"] = os.pathsep.join(
        [
            *required_paths,
            *[item for item in existing_paths if item not in required_paths],
        ]
    )
    child_environment[OFFLINE_ENVIRONMENT_FLAG] = "1"
    return child_environment


def main() -> int:
    original_popen = subprocess.Popen

    def offline_popen(*args: object, **kwargs: object) -> object:
        kwargs["env"] = _child_environment(kwargs.get("env"))
        return original_popen(*args, **kwargs)

    with (
        patch("socket.socket", side_effect=forbid_socket),
        patch("socket.create_connection", side_effect=forbid_socket),
        patch("socket.getaddrinfo", side_effect=offline_getaddrinfo),
        patch("socket.gethostbyname", side_effect=forbid_dns),
        patch("socket.gethostbyname_ex", side_effect=forbid_dns),
        patch("socket.gethostbyaddr", side_effect=forbid_dns),
        patch("socket.getnameinfo", side_effect=forbid_dns),
        patch("socket.getfqdn", side_effect=forbid_dns),
        patch("_socket.getaddrinfo", side_effect=forbid_dns),
        patch("_socket.gethostbyname", side_effect=forbid_dns),
        patch("_socket.gethostbyname_ex", side_effect=forbid_dns),
        patch("_socket.gethostbyaddr", side_effect=forbid_dns),
        patch("_socket.getnameinfo", side_effect=forbid_dns),
        patch("subprocess.Popen", side_effect=offline_popen),
    ):
        suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
