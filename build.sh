#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$DIR/venv"

if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

if [ ! -x "$VENV_DIR/bin/mpy-cross" ] ||
   ! "$VENV_DIR/bin/mpy-cross" --version 2>&1 | grep -q 'v1\.29\.'; then
  echo "Installing the MicroPython 1.29 compiler..."
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip 'mpy-cross==1.29.0.post2'
fi

for name in app upgrader; do
  echo "Building $name.mpy..."
  # -s: embed just "$name.py" as the source name, not this machine's absolute path.
  "$VENV_DIR/bin/mpy-cross" -s "$name.py" -o "$DIR/$name.mpy.tmp" "$DIR/$name.py"
  mv "$DIR/$name.mpy.tmp" "$DIR/$name.mpy"
done
