#!/usr/bin/env bash
# One-command startup: install deps, build frontend, run tests, launch API.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "==> Installing Python dependencies"
pip install -r requirements.txt

echo "==> Installing frontend dependencies and building"
pushd frontend >/dev/null
npm install
npm run build
popd >/dev/null

echo "==> Running test suite with coverage"
pytest tests/ --cov=backend --cov-report=term-missing

echo "==> Starting FastAPI server on http://localhost:8000"
uvicorn backend.main:app --host 0.0.0.0 --port 8000
