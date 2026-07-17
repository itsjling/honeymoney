#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
constraints="${repo_root}/constraints/dev.txt"

"${python_bin}" -m pip install \
  --disable-pip-version-check \
  --constraint "${constraints}" \
  setuptools wheel
"${python_bin}" -m pip install \
  --disable-pip-version-check \
  --no-build-isolation \
  --constraint "${constraints}" \
  --editable "${repo_root}[pdf,dev]"
"${python_bin}" -m pip check
"${python_bin}" "${repo_root}/scripts/check_constraints.py" "${constraints}"
