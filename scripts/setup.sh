#!/usr/bin/env bash
# One-command setup for a new machine: finds a suitable Python, creates the
# venv, installs mediavault, and initializes this machine's local config/index.
# Safe to re-run — every step is skipped if already done.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Looking for a Python interpreter (need 3.10+)..."
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
        major="${version%%.*}"
        minor="${version##*.}"
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$(command -v "$candidate")"
            echo "    using $PYTHON (Python $version)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "No Python 3.10+ found on this machine."
    if command -v brew >/dev/null 2>&1; then
        echo "Installing one via Homebrew (python@3.12)..."
        brew install python@3.12
        PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
    else
        echo "Install Python 3.10+ yourself (e.g. https://python.org/downloads or your package manager), then re-run this script."
        exit 1
    fi
fi

if [ ! -d .venv ]; then
    echo "==> Creating virtualenv (.venv)..."
    "$PYTHON" -m venv .venv
else
    echo "==> .venv already exists, reusing it."
fi

echo "==> Installing mediavault (with the desktop GUI extra)..."
.venv/bin/pip install -q --upgrade pip
if ! .venv/bin/pip install -q -e ".[gui]"; then
    echo "    GUI extra failed to install (pywebview needs extra system deps on some Linux setups) — installing without it."
    echo "    You can still use 'mediavault web'; retry '.venv/bin/pip install -e \".[gui]\"' later for the desktop app."
    .venv/bin/pip install -q -e .
fi

if [ ! -f config.yaml ]; then
    echo "==> First time on this machine — initializing config + local index..."
    .venv/bin/mediavault init
else
    echo "==> config.yaml already exists on this machine, leaving it alone."
fi

echo ""
echo "Done. Next steps:"
echo "  .venv/bin/mediavault root add <label> <path> --role <primary|backup|icloud|inbox> --mode <mirror|subset|watch>"
echo "  .venv/bin/mediavault web     # or: .venv/bin/mediavault gui"
echo ""
echo "Tip: 'source .venv/bin/activate' first if you'd rather type 'mediavault' without the .venv/bin/ prefix."
