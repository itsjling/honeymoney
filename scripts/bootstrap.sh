#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"

"${python_bin}" -m pip install --disable-pip-version-check -e ".[pdf,dev]"
