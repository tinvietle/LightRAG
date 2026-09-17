#!/usr/bin/env bash
set -euo pipefail

# Find the repository from this script's location, so this works from any folder.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# Prefer LightRAG's virtual environment; fall back to python3 if it is absent.
PYTHON="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="python3"
fi

# Extra arguments are forwarded to the Python lesson.
exec "${PYTHON}" "${SCRIPT_DIR}/learn_quickumls_normalization.py" "$@"
