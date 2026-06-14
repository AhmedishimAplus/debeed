#!/bin/bash
# EGY Property Automation — Mac Launcher
# ─────────────────────────────────────────────────────────────────
# Double-click this file in Finder to launch the app.
# First run installs required packages automatically (~1 min).
# ─────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

# ── Python 3 check ───────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    osascript -e 'display dialog "Python 3 is not installed.\n\nGo to python.org, download Python 3, install it, then double-click this file again." buttons {"OK"} default button "OK" with icon stop'
    exit 1
fi

# ── Install playwright if missing ─────────────────────────────────
if ! python3 -c "import playwright" &>/dev/null; then
    echo "Installing playwright (one-time setup)..."
    pip3 install playwright 2>/dev/null || python3 -m pip install playwright
    if ! python3 -c "import playwright" &>/dev/null; then
        osascript -e 'display dialog "Failed to install playwright.\n\nOpen Terminal and run:\n  pip3 install playwright" buttons {"OK"} default button "OK" with icon stop'
        exit 1
    fi
fi

# ── Install Chromium browser if missing ──────────────────────────
BROWSER_DIR="$HOME/Library/Caches/ms-playwright"
if [ ! -d "$BROWSER_DIR" ] || [ -z "$(ls -A "$BROWSER_DIR" 2>/dev/null)" ]; then
    echo "Installing browser engine (one-time, may take 1-2 minutes)..."
    python3 -m playwright install chromium
fi

# ── Launch app ───────────────────────────────────────────────────
echo "Starting EGY Property Automation..."
python3 gui.py
