#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"

"${python_bin}" -m ruff format --check honeymoney tests scripts
"${python_bin}" -m ruff check honeymoney tests scripts
"${python_bin}" -m unittest discover

rm -rf build dist honeymoney.egg-info
"${python_bin}" -m build --no-isolation
