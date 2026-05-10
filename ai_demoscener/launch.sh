#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.8+."
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

echo "Installing/updating dependencies..."
pip install --quiet -r requirements.txt

if [ ! -f "static/three/three.module.js" ]; then
    echo "Downloading three.js vendor files..."
    python download_vendor.py
fi

echo "Starting AI Demoscener..."
python app.py --launch
