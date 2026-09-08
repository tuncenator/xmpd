#!/usr/bin/env bash
# Run through uv so local development and CI use the locked dev dependencies:
# uv run --locked --extra dev bash scripts/check.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python -m ruff check xmpd/
python -m mypy xmpd/
bash scripts/check_version_sync.sh
python -m pytest -q "$@"
