#!/usr/bin/env bash
set -euo pipefail

# Idempotent dependency setup for the Arbitrage Backend (FastAPI) service.
# Runs after the repository is checked out. Safe to run repeatedly.

cd "$(dirname "$0")/.."

# The default image ships python3.12 without the venv module.
if ! dpkg -s python3.12-venv >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12-venv
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
