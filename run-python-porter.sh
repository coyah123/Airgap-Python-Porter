#!/usr/bin/env bash
# Launches the Air-Gapped Python Porter on macOS / Linux with no terminal needed.
# Make executable once:  chmod +x run-python-porter.sh
# Then double-click (or run ./run-python-porter.sh).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer pythonw if present (rare on mac/linux); otherwise python3.
if command -v pythonw >/dev/null 2>&1; then
    exec pythonw "$DIR/porter.py"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 "$DIR/porter.py"
else
    exec python "$DIR/porter.py"
fi
