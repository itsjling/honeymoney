#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

"${python_bin}" -m pip install --disable-pip-version-check \
  -c "${repo_root}/constraints/dev.txt" \
  -e "${repo_root}[pdf,dev]"
