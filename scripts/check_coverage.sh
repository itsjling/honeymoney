#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${repo_root}"
cleanup() {
  "${python_bin}" -m coverage erase
}
trap cleanup EXIT

"${python_bin}" -m coverage erase
"${python_bin}" -m coverage run scripts/run_tests_offline.py
"${python_bin}" -m coverage combine
"${python_bin}" -m coverage report
