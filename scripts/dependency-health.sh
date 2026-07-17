#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${python_bin}" -m pip check
"${python_bin}" "${repo_root}/scripts/check_constraints.py" \
  "${repo_root}/constraints/dev.txt"
"${python_bin}" -m pip_audit \
  --requirement "${repo_root}/constraints/dev.txt" \
  --no-deps \
  --disable-pip \
  --progress-spinner=off \
  --strict
