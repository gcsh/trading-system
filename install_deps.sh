#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/pip install -r requirements.txt
echo "DONE"
