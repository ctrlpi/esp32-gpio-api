#!/usr/bin/env bash
# Usage: curl -fsSL https://raw.githubusercontent.com/ctrlpi/esp32-gpio-api/main/install.sh | bash
#
# Deploys boot.py, app.mpy, main.py and device settings to every ESP32 on USB.
# Runs on your Mac/PC - MicroPython has no shell to run this on the device itself.
# Downloads the release files when piped into a shell or run with --upgrade; a plain
# ./install.sh deploys the files already on disk. Prompts only for what the device lacks.
#
#   ./install.sh              # full deploy: boot.py + app.mpy + main.py + config/wifi as needed
#   ./install.sh --config     # re-ask for API key/agent name even if the device already has config.json
#   ./install.sh --wifi       # re-ask for WiFi SSID/password even if the device already has wifi.txt
set -euo pipefail

BASE_URL="https://raw.githubusercontent.com/ctrlpi/esp32-gpio-api/main"
FILES="app.py app.mpy main.py boot.py build.sh install.sh LICENSE README.md"
DIR="esp32-gpio-api"
SHELLS=(bash sh dash zsh ksh ash)

# Piped into a shell (curl ... | bash), $0 is the shell itself: download the release.
RUN_AS="$(basename "$0")"
DOWNLOAD=false
for s in "${SHELLS[@]}"; do
  [ "$RUN_AS" = "$s" ] && DOWNLOAD=true
done
FORCE_WIFI=false
FORCE_CONFIG=false
for arg in "$@"; do
  [ "$arg" = "--upgrade" ] && DOWNLOAD=true
  [ "$arg" = "--wifi" ] && FORCE_WIFI=true
  [ "$arg" = "--config" ] && FORCE_CONFIG=true
done

if [ "$DOWNLOAD" = true ] && ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required. Install it first (e.g. brew install curl)." >&2
  exit 1
fi

# Already inside the project folder (main.py + install.sh present, whatever the
# folder's named)? Update in place instead of nesting a new copy inside it.
if [ "$(basename "$PWD")" = "$DIR" ] || { [ -f main.py ] && [ -f install.sh ]; }; then
  DIR="."
else
  mkdir -p "$DIR"
fi

if [ "$DOWNLOAD" = true ]; then
  for f in $FILES; do
    echo "Downloading $f..."
    curl -fsSL "$BASE_URL/$f" -o "$DIR/$f.tmp"
    mv "$DIR/$f.tmp" "$DIR/$f"   # only replace the old copy once the download completed
  done
  chmod +x "$DIR/install.sh" "$DIR/build.sh"
fi

for f in app.py main.py boot.py build.sh; do
  if [ ! -f "$DIR/$f" ]; then
    echo "ERROR: missing $DIR/$f. Restore the local file or run ./install.sh --upgrade." >&2
    exit 1
  fi
done

# --- venv + USB tool ---
VENV_DIR="$DIR/venv"
# Recreate the venv if it's missing or broken (e.g. an interrupted creation leaves
# a dangling bin/python).
if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
  [ -d "$VENV_DIR" ] && echo "Rebuilding deployment environment..." || echo "Creating deployment environment..."
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
if [ ! -x "$VENV_DIR/bin/mpremote" ]; then
  echo "Installing mpremote..."
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip mpremote
fi
MPREMOTE="$VENV_DIR/bin/mpremote"

if [ ! -f "$DIR/app.mpy" ] || [ "$DIR/app.py" -nt "$DIR/app.mpy" ]; then
  "$DIR/build.sh"
fi

ask() {
  # $1=prompt $2=default. Skipped with no /dev/tty (falls back to default).
  local ans=""
  if { : < /dev/tty; } 2>/dev/null; then
    read -r -p "$1 [$2]: " ans < /dev/tty || ans=""
  fi
  printf '%s' "${ans:-$2}"
}

ask_optional() {
  # $1=prompt, no default - blank is a valid answer. Skipped with no /dev/tty.
  local ans=""
  if { : < /dev/tty; } 2>/dev/null; then
    read -r -p "$1: " ans < /dev/tty || ans=""
  fi
  printf '%s' "$ans"
}

ask_required() {
  # $1=label. No default (an SSID/password can't be guessed) - loops until
  # non-empty, or errors out if there's no terminal at all.
  local label="$1" ans=""
  if ! { : < /dev/tty; } 2>/dev/null; then
    echo "ERROR: no terminal available to ask for $label, and the ESP32 has no wifi.txt." >&2
    echo "Create $DIR/wifi.txt by hand (see wifi.txt.example), push it with" >&2
    echo "$MPREMOTE connect <serial-port> fs cp $DIR/wifi.txt :wifi.txt, then re-run." >&2
    exit 1
  fi
  while [ -z "$ans" ]; do
    read -r -p "$label: " ans < /dev/tty || ans=""
  done
  printf '%s' "$ans"
}

# --- find connected ESP32 boards (checks below query the first one) ---
shopt -s nullglob
CANDIDATES=(/dev/cu.usbserial-* /dev/cu.usbmodem* /dev/ttyUSB* /dev/ttyACM*)
shopt -u nullglob
DEVICES=()
if [ "${#CANDIDATES[@]}" -gt 0 ]; then
  for DEV in "${CANDIDATES[@]}"; do
    if BOARD_OS="$("$MPREMOTE" connect "$DEV" exec "import os; print(os.uname().sysname)" 2>/dev/null)" &&
       [ "$(printf '%s' "$BOARD_OS" | tr -d '\r\n')" = "esp32" ]; then
      DEVICES+=("$DEV")
    fi
  done
fi
if [ "${#DEVICES[@]}" -eq 0 ]; then
  echo "ERROR: no MicroPython ESP32 found on USB." >&2
  exit 1
fi
echo "Found ${#DEVICES[@]} ESP32 device(s): ${DEVICES[*]}"

# --- config.json: API key + agent name. Only asked for when the *device* doesn't
# already have one (--config forces the prompt). A blank name keeps the device's own
# unique device fallback, which matters when flashing several ESP32 boards at once.
CONFIG_FILE="$DIR/config.json"
WIFI_FILE="$DIR/wifi.txt"
trap 'rm -f "$CONFIG_FILE" "$WIFI_FILE"' EXIT

NEED_CONFIG=false
if [ "$FORCE_CONFIG" = true ]; then
  NEED_CONFIG=true
elif ! "$MPREMOTE" connect "${DEVICES[0]}" fs cat :config.json >/dev/null 2>&1; then
  NEED_CONFIG=true
fi
if [ "$NEED_CONFIG" = true ]; then
  if [ -s "$CONFIG_FILE" ] && [ "$FORCE_CONFIG" = false ]; then
    echo "Using existing $CONFIG_FILE"
  else
    echo "Agent config not found on the device."
    API_KEY="$(ask "API key" "your-secret-key")"
    AGENT_NAME="$(ask_optional "Agent name [blank = device default, esp32-<serial4>]")"
    if [ -n "$AGENT_NAME" ]; then
      printf '{\n  "api_key": "%s",\n  "name": "%s"\n}\n' "$API_KEY" "$AGENT_NAME" > "$CONFIG_FILE"
    else
      printf '{\n  "api_key": "%s"\n}\n' "$API_KEY" > "$CONFIG_FILE"
    fi
    echo "Saved $CONFIG_FILE"
  fi
fi

# --- wifi.txt: SSID + password, no default. Only asked for if the device
# doesn't already have one; --wifi forces the prompt regardless.
NEED_WIFI=false
if [ "$FORCE_WIFI" = true ]; then
  NEED_WIFI=true
elif ! "$MPREMOTE" connect "${DEVICES[0]}" fs cat :wifi.txt >/dev/null 2>&1; then
  NEED_WIFI=true
fi
if [ "$NEED_WIFI" = true ]; then
  if [ -s "$WIFI_FILE" ] && [ "$FORCE_WIFI" = false ]; then
    echo "Using existing $WIFI_FILE"
  else
    echo "WiFi credentials not found on the device."
    SSID="$(ask_required "WiFi SSID")"
    PASSWORD="$(ask_required "WiFi password")"
    printf 'SSID = %s\nPASSWORD = %s\n' "$SSID" "$PASSWORD" > "$WIFI_FILE"
    echo "Saved $WIFI_FILE"
  fi
fi

# --- scripts/cpu-temp.py: seed an ESP32 chip-temperature sensor only when the
# device does not already have one, so user changes survive later installs.
SCRIPT_FILE="$DIR/scripts/cpu-temp.py"
NEED_SCRIPT=false
REMOTE_SCRIPT=""
if ! REMOTE_SCRIPT="$("$MPREMOTE" connect "${DEVICES[0]}" fs cat :scripts/cpu-temp.py 2>/dev/null)"; then
  NEED_SCRIPT=true
elif printf '%s' "$REMOTE_SCRIPT" | grep -q 'machine\.ADC(4).*read_u16'; then
  echo "Replacing the Pico cpu-temp.py template on the device."
  NEED_SCRIPT=true
fi
if [ "$NEED_SCRIPT" = true ]; then
  mkdir -p "$DIR/scripts"
  # Staged only to copy it over, so it goes on exit like config.json/wifi.txt.
  trap 'rm -f "$CONFIG_FILE" "$WIFI_FILE" "$SCRIPT_FILE"; rmdir "$DIR/scripts" 2>/dev/null || true' EXIT
  cat > "$SCRIPT_FILE" <<'SCRIPT'
# ESP32 internal chip temperature in degrees Celsius.
# This measures the MCU die, which normally runs warmer than ambient air.
import esp32

def read():
    if hasattr(esp32, "mcu_temperature"):
        return round(esp32.mcu_temperature(), 1)
    fahrenheit = esp32.raw_temperature()
    return round((fahrenheit - 32) * 5 / 9, 1)
SCRIPT
  echo "Saved $SCRIPT_FILE - configure it with {\"script\": \"cpu-temp.py\"}"
fi

# --- deploy to every connected ESP32 ---
FAILED=0
for DEV in "${DEVICES[@]}"; do
  echo "Deploying to $DEV..."
  CMD_ARGS=("connect" "$DEV")
  if [ "$NEED_SCRIPT" = true ]; then
    CMD_ARGS+=("exec" "import os; 'scripts' not in os.listdir() and os.mkdir('scripts')" "+")
  fi
  CMD_ARGS+=("fs" "cp" "$DIR/boot.py" ":boot.py" "+")
  CMD_ARGS+=("fs" "cp" "$DIR/app.mpy" ":app.mpy" "+")
  CMD_ARGS+=("fs" "cp" "$DIR/main.py" ":main.py")
  if [ "$NEED_CONFIG" = true ]; then
    CMD_ARGS+=("+" "fs" "cp" "$CONFIG_FILE" ":config.json")
  fi
  if [ "$NEED_WIFI" = true ]; then
    CMD_ARGS+=("+" "fs" "cp" "$WIFI_FILE" ":wifi.txt")
  fi
  if [ "$NEED_SCRIPT" = true ]; then
    CMD_ARGS+=("+" "fs" "cp" "$SCRIPT_FILE" ":scripts/cpu-temp.py")
  fi
  CMD_ARGS+=("+" "reset")

  if ! "$MPREMOTE" "${CMD_ARGS[@]}"; then
    echo "  FAILED: $DEV - continuing with remaining devices" >&2
    FAILED=1
  fi
done

[ "$FAILED" = 0 ] && echo "Done." || exit 1

