# ESP32 GPIO API

[![Platform: ESP32](https://img.shields.io/badge/platform-ESP32-E7352C.svg)](#install-on-your-esp32)
[![Version 0.9.26](https://img.shields.io/badge/version-0.9.26-blue.svg)](https://github.com/ctrlpi/esp32-gpio-api/tags)
[![MicroPython 1.29](https://img.shields.io/badge/runtime-MicroPython%201.29-blue.svg)](#install-on-your-esp32)
[![Auth: Api-Key](https://img.shields.io/badge/auth-Api--Key-orange.svg)](#authentication)
[![License: MIT](https://img.shields.io/badge/license-MIT-188038.svg)](LICENSE)
![Status: Beta](https://img.shields.io/badge/status-Beta-red.svg)

A MicroPython server that exposes ESP32 GPIO control through a REST API.

Runs on an ESP32, ESP32-C3 or ESP32-S3 with MicroPython 1.29, API-key authentication, and `machine.Pin` GPIO.

It is the ESP32 port of [pi-gpio-api](https://github.com/ctrlpi/pi-gpio-api), with the same endpoints and wire format.

> Looking for AI-agent / MCP control, Apple HomeKit or Google Home? See the [Related projects](#related-projects) below that drive one or more of these API servers (Pi, Pico, or ESP32) over their REST endpoints.

## Install on your ESP32

**Requirements:** MicroPython 1.29 on an ESP32, ESP32-C3 or ESP32-S3 (the [ESP32_GENERIC](https://micropython.org/download/ESP32_GENERIC/), [ESP32_GENERIC_C3](https://micropython.org/download/ESP32_GENERIC_C3/) or [ESP32_GENERIC_S3](https://micropython.org/download/ESP32_GENERIC_S3/) firmware), Python 3, and a Mac or Linux computer.


Run `install.sh` on the computer with the ESP32 connected over USB.

### Option 1: curl (recommended)

One-liner, no git required - downloads the release files into `./esp32-gpio-api`:

```bash
curl -fsSL https://raw.githubusercontent.com/ctrlpi/esp32-gpio-api/main/install.sh | bash
```


`install.sh` preserves device `config.json` and `wifi.txt`, asking only for missing values. It deploys every detected USB ESP32 and removes locally staged credentials afterward.

Re-running `./install.sh` uses local files. It runs `build.sh` only when `app.mpy` is missing or older than `app.py`.

- `./install.sh --config`: re-ask for the API key/agent name and push it, even if the device already has `config.json`.
- `./install.sh --wifi`: re-ask for WiFi SSID/password and push it, even if the device already has `wifi.txt`.
- `./install.sh --upgrade`: download all release files, including `app.mpy`, before deployment.

### Option 2: git clone

```bash
git clone https://github.com/ctrlpi/esp32-gpio-api.git
cd esp32-gpio-api
./install.sh
```

Or copy `boot.py`, `app.mpy`, `main.py`, and `wifi.txt` to the ESP32 manually.

```
SSID = YourNetwork
PASSWORD = YourPassword
```

`boot.py` reads this file before loading the application. The API key defaults to `"your-secret-key"`; change it via `POST /config/update` or `config.json`.

### Run automatically at boot

MicroPython runs `boot.py` and `main.py` automatically. `boot.py` reserves Wi-Fi memory, then `main.py` loads `app.mpy`.

The server binds **`0.0.0.0`** (all interfaces) and port **8314**, so it is reachable on your local network; keep it behind a trusted LAN (see the No-TLS note under Authentication).

## Try the API

Replace `<esp32-ip>` with the board address and `your-secret-key` with your API key.

#### 1. Health check

```bash
curl http://<esp32-ip>:8314/hello
```

#### 2. Read all configured pins

```bash
curl http://<esp32-ip>:8314/gpio/read -H "Api-Key: your-secret-key"
```

#### 3. Write to a pin

```bash
curl -X POST http://<esp32-ip>:8314/gpio/write/26 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"value": 1, "duration": 2}'
```

#### 4. Configure a pin (relay, reversed, max 1 hour)

```bash
curl -X POST http://<esp32-ip>:8314/gpio/config/26 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"name": "relay", "type": "output", "reversed": true, "max": 3600}'
```

#### 5. Scan all usable GPIO pins

```bash
curl http://<esp32-ip>:8314/gpio/scan -H "Api-Key: your-secret-key"
```

#### 6. Configure a watched input (fires a webhook on change)

```bash
curl -X POST http://<esp32-ip>:8314/gpio/config/18 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"type": "input", "watched": true}'
```

#### 7. Change the webhook URL

```bash
curl -X POST http://<esp32-ip>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://example.com/webhook"}'
```

#### 8. Configure a sensor

```bash
# cpu-temp.py is the example script install.sh seeds into scripts/
curl -X POST http://<esp32-ip>:8314/sensor/config/cpu_temp \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"script": "cpu-temp.py"}'
```

#### 9. Read a sensor

```bash
curl -H "Api-Key: your-secret-key" http://<esp32-ip>:8314/sensor/read/cpu_temp
```

## Authentication

All endpoints require an `Api-Key` header except `/hello`.

```
Api-Key: your-secret-key
```

> **No TLS.** The server speaks plain HTTP, so the key travels as plaintext on the wire. That's fine on a trusted LAN, but don't expose port 8314 directly to the internet. If you need remote access, tunnel it.

## REST API

Identical wire format and behavior to [pi-gpio-api](https://github.com/ctrlpi/pi-gpio-api). The ESP32 has no interactive `/docs` or `/redoc`.

## Health

A single unauthenticated endpoint for discovery and liveness checks; it reports the agent's name.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/hello` | none | Returns `{"name": "agent-name"}`, used for discovery and health checks. If name is not configured will return `esp32-<serial4>` (e.g. `esp32-1a2b`), built from the last 4 of the chip UID. |

## GPIO

The core of the API: read, write, configure, and watch GPIO pins. Every pin is addressed by its GPIO number or a configured name alias.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/gpio/read` | Read all configured pins |
| `GET` | `/gpio/read/{name_or_gpio}` | Read a single pin by name alias or GPIO number |
| `POST` | `/gpio/write/{name_or_gpio}` | Write a value to a pin |
| `POST` | `/gpio/config/{name_or_gpio}` | Configure a pin (name, type, init, pullup, max, reversed, watched) |
| `GET` | `/gpio/watched` | List all currently watched pins, the active webhook URL, and the `bridges` callback URLs (URLs only, never the keys) |
| `GET` | `/gpio/scan` | Scan the usable GPIOs of the chip it runs on: classic ESP32 0, 2, 4, 5, 12-15, 18, 19, 21-23, 25-27 and 32-39 (GPIO34-39 are input-only and have no internal pull resistors); ESP32-C3 0-10, 20 and 21; ESP32-S3 0-18, 21 and 38-48. Flash, PSRAM, USB and REPL pins are excluded. Returns physical `level`, logical `value`, and the live `agent` block. |

**Status LED.** The agent pulses the board's LED to show it is running and on WiFi: GPIO2 on a classic ESP32, GPIO8 on an ESP32-C3 (active low), and the WS2812 RGB LED on GPIO48 on an ESP32-S3 (green). Reading or scanning that pin never reconfigures it, so the pulse keeps going.

### Request Bodies

#### `POST /gpio/write/{name_or_gpio}`

```json
{
  "value": 1,
  "duration": 2.5
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `value` | `0`, `1`, `"on"`, `"off"`, `"toggle"` | Value to write. `"on"`/`"off"` are word spellings of `1`/`0`, `"toggle"` flips the current state. Strings are case-insensitive. A JSON boolean (`true`/`false`) also works, coerced to `1`/`0`. Any other integer counts as `1` if nonzero, `0` only if exactly `0` (so a negative value like `-1`, used by some systems for true, also means "on"). |
| `duration` | float (seconds) | Optional. Pulse the pin for this many seconds, then revert. If the pin has `max` configured, a larger `duration` is clamped to `max`, and a write **without** `duration` uses `max` as the duration (it pulses and reverts, it does not latch). |

Writing to an **unconfigured** pin auto-configures it as an output first. A pin already configured as anything else (`input`, `vcc`, `gnd`) is never silently flipped; the write returns `400`. Reconfigure it as `output` via `/gpio/config` first.

#### `POST /gpio/config/{name_or_gpio}`

```json
{
  "name": "relay",
  "type": "output",
  "init": 0,
  "pullup": "up",
  "reversed": true,
  "max": 5.0,
  "watched": true
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `name` | string | Name alias for the pin, usable in place of the GPIO number in all endpoints. |
| `type` | `input`, `output`, `vcc`, `gnd`, `remove` | Pin direction. `vcc`/`gnd` are permanently driven and cannot be written or watched. `remove` deletes the pin config. Changing type clears fields that no longer apply (see below). |
| `init` | `0`, `1`, `"on"`, `"off"`, `"last"`, `"restore"` | Startup behavior, also applied on every live `/gpio/config` call. **Outputs**: drive to value. Inputs: compare to reference and fire on mismatch. `"on"`/`"off"` are word spellings of `1`/`0`, and `"restore"` is a synonym for `"last"`, which uses the value from the last run. Strings are case-insensitive. A JSON boolean (`true`/`false`) also works, coerced to `1`/`0`. Any other integer counts as `1` if nonzero, `0` only if exactly `0` (so `2` and `-1` both mean "on", matching `/gpio/write`). All of this is stored canonically, so `/config/read` and `/gpio/read` always answer `0`, `1` or `"last"`. Unknown words are **rejected**, not stored and ignored. On an input the reference is compared against the pin's reported (logical) value, so `pullup` and `reversed` are already accounted for. If omitted on an output: a pin that's already configured as output keeps its current value unchanged; a pin that's *newly* becoming an output (was `input`, `vcc`, `gnd` or unconfigured) starts at `0`. Cleared automatically when the pin's `type` changes. |
| `pullup` | `"up"`, `"down"`, `"none"` | Pull resistor (inputs only). Cleared automatically when type is set to `output`, `vcc`, or `gnd`. |
| `max` | float (seconds) | Maximum pulse duration (outputs only). Writes with a larger `duration` are clamped to it, and writes with **no** `duration` use it as the duration (see `POST /gpio/write` above). Cleared automatically when type is set to `input` or when type is set to `output` if the value is `0` or unset. |
| `reversed` | boolean | Inverts the logical value. **Output**: writes the boolean flip to the physical GPIO pin (physical HIGH = logical 0). **Input**: flips the reported value. Cleared automatically when type is set to `vcc` or `gnd`. |
| `watched` | boolean | `true` to send a webhook on every write (outputs) or state change (inputs); `false` to stop. Cleared automatically when type is set to `vcc` or `gnd`. Webhooks are only sent when a `webhook_url` is configured. With none set, each watched event is recorded in the agent log instead (see [Webhooks](#webhooks)). |

## Sensors

A **sensor** is a named reading the agent computes on demand by running a script, a way to surface values GPIO pins can't carry (I2C sensors, 1-Wire probes, an on-chip metric, the output of any MicroPython module).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sensor/read` | Read every configured sensor, merged into one JSON object |
| `GET` | `/sensor/read/{name}` | Read one sensor by name |
| `POST` | `/sensor/config/{name}` | Configure a named sensor |

### Request Bodies

#### `POST /sensor/config/{name}`

```json
{
  "script": "cpu-temp.py"
}
```

Or, to delete the sensor:

```json
{
  "remove": true
}
```

Sensors are pure config plus on-demand reads: no pins, no background polling, no extra persisted state beyond `config.json`.

### Configure

Scripts live in the ESP32's **`scripts/` folder**. `install.sh` creates it and seeds `cpu-temp.py`; upload another script with `mpremote connect <port> fs cp weather.py :scripts/weather.py`.

The ESP32 has no shell, so a sensor script is a MicroPython `.py` file executed in-process. Its `script` value is `"<file.py> [args...]"`; the file must be a bare filename in `scripts/`. It should define `read(*args)` or set `result`. Arguments may contain letters and digits only, and results may be any JSON-serializable value.

```bash
# The same script with arguments (letters and digits only)
curl -X POST http://<esp32-ip>:8314/sensor/config/zone1 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"script": "weather.py zone1"}'
```

`scripts/` is the only folder a sensor can run code from. Paths, `..`, hidden files, and subfolders are rejected.

Reading returns a clear message instead of failing when a script is missing or broken: `{"<name>": "script not found"}`, `{"<name>": "script failed or did not produce output"}` when the script errors or its `read()`/`result` yields nothing, and `{"<name>": "blocked: <reason>"}` when a sensor violates the name/argument rules (possible when sensors arrive via `/config/load` or an edited `config.json`; every read re-validates).

### Read

`GET /sensor/read/{name}` reads one sensor; `GET /sensor/read` reads them all, merged into one object. The output is always JSON, and a reading is **always nested under the sensor's own name** - one key per sensor, whatever the script returned:

- A JSON object with **several fields** nests whole: `{ "<name>": { ... } }`.
- A JSON object with a **single field** contributes just that field's value: `{ "<name>": <value> }` (the field's own key is dropped - a script that returns `{"temp": 47.8}` for a sensor named `cpu_temp` reads back as `{"cpu_temp": 47.8}`).
- Anything else (a non-JSON answer such as a bare number or string) is wrapped the same way; numbers stay numbers - a script returning `47.8` for a sensor named `cpu_temp` reads back as `{ "cpu_temp": 47.8 }` (a number, not a string), and a script returning `ok` for a sensor named `status` reads back as `{ "status": "ok" }`.

Because every sensor owns exactly one key in that merge, two sensors emitting the same field name can never overwrite each other. An unknown sensor name returns `404`.

```bash
curl -H "Api-Key: your-secret-key" http://<esp32-ip>:8314/sensor/read
# → {"weather": {"pressure": 1013, "humidity": 62}, "probe": 21.4}
```

## Agent Setup

Read and change the agent's own configuration, and drive process and device lifecycle (restart, upgrade, logs).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config/read` | Read the agent identity + full config (see response fields below) |
| `POST` | `/config/update` | Update one or more config fields (backs up the current config first) |
| `POST` | `/config/load` | Load a full config from a JSON dict (backs up the current config first; pass `{}` to reset) |
| `POST` | `/upgrade` | Download and transactionally install `app.mpy`, `boot.py`, and `main.py`, then reset |
| `POST` | `/restart` | Restart the device. Body is optional; pass `{"reboot": true}` to request a full hardware reset instead (see below) |
| `GET` | `/logs` | Last 30 log lines as a JSON array (in-memory ring buffer, cleared on reboot) |

**Restart/reboot (`/restart`)**: a plain `/restart` reloads the agent in place (config, pins and watches are set up again, and Wi-Fi stays connected). `{"reboot": true}` does a full hardware reset with `machine.reset()`. Both reply first, with `{"status": "restarting"}` or `{"status": "rebooting"}`.

**Self-upgrade (`/upgrade`)** downloads `app.mpy`, `boot.py`, and `main.py` from the GitHub release branch. It verifies every download before replacing anything, backs up each installed file, verifies the installed copies, and restores the previous set if installation fails. Configuration, Wi-Fi credentials, status, and sensor scripts are untouched.

The response is sent before the ESP32 resets and reconnects.

### `GET /config/read` response

Returns the agent's identity plus its full config:

| Field | Description |
|-------|-------------|
| `name` | Configured name, otherwise `esp32-<serial4>`. Matches `/hello`. |
| `agent` | Live facts: `platform`, MicroPython `os`, `host` (`"esp32"`), chip `serial`, `ip`, Wi-Fi `mac`, SSID, signal strength, internal chip temperature in °C, uptime, API version, and config modification time. This block is computed on read and never stored. |
| `notifications` | The general-purpose webhook target: `webhook` and `webhook_key`. |
| `bridges` | The three bridge callbacks: `homekit`, `homekit_key`, `matter`, `matter_key`, `homebridge`, `homebridge_key` (see [Bridge callbacks](#bridge-callbacks-bridgeshomekit--bridgesmatter--bridgeshomebridge)). |
| `settings` | Operational toggles: `logs_enabled`, `log_days`. |
| `sensors` | Named-sensor configuration map (see [Sensors](#sensors)). |
| `gpios` | Per-pin configuration map (see [GPIO](#gpio); always the last field). |

### Config fields (`/config/update`)

Send fields **ungrouped, at the top level** - the agent routes each one into its group,
so a later `/config/read` shows them nested:

```json
{
  "name": "my-esp32",
  "webhook_url": "https://example.com/webhook",
  "webhook_key": "",
  "matter": "",
  "matter_key": "",
  "homekit": "",
  "homekit_key": "",
  "homebridge": "",
  "homebridge_key": "",
  "logs_enabled": true,
  "log_days": 7
}
```

| Sent at the root | Lands in |
|---|---|
| `webhook_url`, `webhook_key` | `notifications.webhook`, `notifications.webhook_key` |
| `matter`, `matter_key`, `homekit`, `homekit_key`, `homebridge`, `homebridge_key` | `bridges.*` |
| `logs_enabled`, `log_days` | `settings.*` |
| `name`, `api_key` | top level, ungrouped |

The grouped spelling works too (`{"settings": {"log_days": 5}}`), and either way each group
**merges key by key** - setting `log_days` never resets `logs_enabled`, and a bridge pushing
its own URL never clears the other bridge's. `/config/load` accepts both forms as well, and
always writes `config.json` in the grouped shape.

Keys, names, URLs and scripts are always text: a number sent for one is stored as text
(`4242` becomes `"4242"`), on `/config/update`, `/config/load`, `/gpio/config` and `/sensor/config`.

`webhook_key` is a dedicated key for the webhook *target* (see Webhooks below); it is not the agent's own `api_key`. An empty string clears it, same as `webhook_url`.

The live `agent` block is read-only: if it is present in an update or a whole-config load it is dropped rather than stored, so a read-modify-load round trip can post `/config/read` straight back.

Log retention is controlled by the `log_days` config field (default `7`, range `0`-`30`, clamped). An hourly background pass trims the in-memory log buffer down to that many days; setting `log_days` to `0` clears the buffer immediately and then keeps only the last hour on each pass.

## Errors

Error responses are JSON `{"error": ...}`, except 404 which has an empty body:

| Status | When | Body |
|--------|------|------|
| `400` | Bad write/config or a request over **8 KB** | `{"error": "<reason>"}`; oversized requests return `{"error": "Bad request"}` |
| `403` | Missing or wrong `Api-Key` header | `{"error": "Invalid or missing Api-Key header"}` |
| `404` | Unknown pin/name, or unknown route | empty |
| `422` | Malformed JSON or a missing required field | `{"error": "<reason>"}` |

## Webhooks

### Payload

When a pin has `watched: true`, the server POSTs the pin's state to `webhook_url` on every write (outputs) or state change (inputs: every edge, the primary use case):

```json
{ "agent": "esp32-1a2b", "gpio": 17, "value": 1, "type": "output", "name": "relay" }
```

For reversed pins the payload also includes `level` (the physical GPIO state).

No webhook URL is configured by default. When a watched event occurs with **no target at all** configured (no `webhook_url` and no bridge callback), nothing is sent; the event is recorded in the agent log instead.

### HTTPS targets

HTTPS webhooks work when the installed MicroPython firmware includes SSL support. TLS needs substantially more memory than plain HTTP, so keep local bridge callbacks on HTTP where possible.

### Delivery and retries

A failed delivery is retried **twice more, 30 seconds apart** (3 attempts in total, per target). A
non-2xx answer counts as a failure just like a refused connection - which is what carries an event
across a bridge restart: the bridge answers `401` until it has re-pushed its key onto this agent
(it does that off the first rejected event), so the retry 30s later is the one that lands. Each
attempt is logged, with the reason and whether another is coming.

### Bridge callbacks (`bridges.homekit` / `bridges.matter` / `bridges.homebridge`)

Three extra callback URLs, one owned by each smart-home bridge, sit alongside `webhook_url`:
`homekit` for the standalone `homekit-bridge`, `matter` for `matter-bridge`, and `homebridge`
for the `homebridge-ctrlpi` plugin. They are **additive**: a watched event is POSTed to every
configured target, with the same payload, so the bridges and `webhook_url` never compete.

`homebridge` has its own slot, so the Homebridge plugin and the standalone `homekit-bridge`
can drive the same agent at once.

```bash
curl -X POST http://<host>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"matter": "http://192.168.1.50:8317/webhook?agent=esp32"}'
```

`homekit-bridge`, `matter-bridge` and `homebridge-ctrlpi` set these themselves at startup, so
you normally never touch them by hand. An empty string clears one. All three are reported by `/config/read` and
`/gpio/watched` under `bridges` (the latter carries the URLs only, never the keys). The group
**merges key by key**, so setting one bridge's fields never disturbs another's.

Each callback has its own key field, `homekit_key` / `matter_key` / `homebridge_key`, set in
the same call:

```bash
curl -X POST http://<host>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"matter": "http://192.168.1.50:8317/webhook?agent=esp32", "matter_key": "Kp3xR9tLmQ7z"}'
```

When set, that key is sent as the `Api-Key` header on that bridge's callbacks (and nowhere
else) so the bridge can tell a real event from one anybody on the LAN made up - it answers
`401` and logs the attempt otherwise. The bridges generate a random 12-character key per
agent on every start and push it here themselves, so a key left behind in this file is
worthless the moment the bridge restarts. As with `webhook_key`, this is never the
agent's own `api_key`: that key can never leak to a bridge. An empty string clears one, which
goes back to unauthenticated callbacks.

### Boot notification

On startup, the server also POSTs a one-off boot notification to every configured target:

```json
{ "agent": "esp32-1a2b", "note": "Up and running" }
```

It carries no pin fields; consumers of the webhook feed should treat entries with a `note` field as status messages, not pin events. The same `webhook_key` rule below applies.

### Authentication

If a dedicated `webhook_key` is configured, it is sent as the `Api-Key` header on every webhook POST; if not, no `Api-Key` header is sent at all. The agent's own `api_key` is never sent to the webhook target. The public `https://ctrlpi.com/webhook/test` test endpoint requires no key, it will display whatever key was sent for debugging.

### Testing webhooks

For development and testing, point `webhook_url` at `https://ctrlpi.com/webhook/test` and monitor [https://ctrlpi.com/webhook/watch](https://ctrlpi.com/webhook/watch). The feed is public and events are retained briefly.

### Changing the URL

Change it by setting `webhook_url` (and optionally `webhook_key`) via `POST /config/update`:

```bash
curl -X POST http://<esp32-ip>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://example.com/webhook", "webhook_key": "target-key"}'
```

## Files

The repository contains source, build, deployment, and device runtime files.

| File | Contents |
|------|----------|
| `app.py` | Application source: Wi-Fi, HTTP API, GPIO, sensors, webhooks, state, and OTA upgrades |
| `app.mpy` | Precompiled application deployed to the ESP32 |
| `boot.py` | Starts Wi-Fi before the application loads |
| `main.py` | Small loader that imports `app.mpy` |
| `build.sh` | Builds `app.mpy` with MicroPython 1.29 `mpy-cross` |
| `install.sh` | Builds when needed, collects missing settings, and deploys over USB |
| `wifi.txt` | WiFi credentials (not committed; see `wifi.txt.example`) |
| `config.json` | API key, pin configs, feature flags, webhook URL. Config backup is written automatically to `config-backup.json` and read on boot if `config.json` exists but fails to parse/validate |
| `status.json` | Last known pin values, restored on boot when `init: "last"`. Inputs are recorded too, so a watched input's `init: "last"` reference has a previous reading to compare against |
| `scripts/` | Sensor scripts; `install.sh` seeds ESP32 chip-temperature support |

## Differences from the Pi version

| Feature | Pi (`pi-gpio-api`) | ESP32 (`esp32-gpio-api`) |
|---------|-----|------|
| GPIO range | BCM 2-27 | Per chip: classic ESP32 0, 2, 4, 5, 12-15, 18, 19, 21-23, 25-27, 32-39 (34-39 input-only); C3 0-10, 20, 21; S3 0-18, 21, 38-48 |
| GPIO | `gpiozero` | `machine.Pin` |
| Sensor scripts | `.sh`, run as `sh scripts/<file>` | `.py`, exec'd in-process |
| Interactive API docs (`/docs`, `/redoc`) | Yes (`settings.docs_enabled`) | No |
| Logs | File (`agent.log`), last 50 lines via `/logs` | In-memory ring buffer, last 30 lines via `/logs`, cleared on reboot |
| HTTPS webhook targets | Always available | Available with SSL-enabled firmware |

## Tests

Run the dependency-free test suite with:

```bash
python3 test.py
```

The test suite runs `app.py` locally with mock MicroPython modules (`test_mocks/`), so no board is needed, and tests its REST API: authentication, configuration read/update/load, GPIO configuration, reading/writing/toggling pin values, duration pulses, watched pins, the status LED pin, GPIO scan, sensor scripts and their security validation, request size limits, and the Wi-Fi watchdog.

## Related projects

- **[`pi-gpio-api`](https://github.com/ctrlpi/pi-gpio-api)**: Raspberry Pi GPIO REST server that reads inputs and drives outputs (and named sensors) over an HTTP API.
- **[`pico-gpio-api`](https://github.com/ctrlpi/pico-gpio-api)**: Raspberry Pi Pico W port of the GPIO REST server, with the same endpoints, wire format, and auth.
- **[`mcp-bridge`](https://github.com/ctrlpi/mcp-bridge)**: MCP server that exposes GPIO control as tools for AI agents (e.g. Claude), executing them over the agents' REST API.
- **[`homebridge-ctrlpi`](https://github.com/ctrlpi/homebridge-ctrlpi)**: A Homebridge plugin that brings Raspberry Pi, Pico W, and ESP32 GPIO into Apple Home, by driving one or more GPIO agents over their REST API.
- **`homekit-bridge`** *(coming soon)*: Native Apple HomeKit bridge that exposes GPIO pins and sensors as HomeKit accessories, driven over the agents' REST API.
- **`matter-bridge`** *(coming soon)*: Matter bridge that exposes GPIO pins and sensors to any Matter platform (Apple Home, Google Home, Alexa, Home Assistant), driven over the agents' REST API.
- **[`ctrlpi-cli`](https://github.com/ctrlpi/ctrlpi-cli)**: Command-line interface for interacting with ctrlPi agents, with shared agent configuration.
- **[`ctrlpi-lab`](https://github.com/ctrlpi/ctrlpi-lab)**: A local browser dashboard for your ctrlPi setup. Test GPIO and sensor endpoints, watch webhook events, and tail agent logs.
- **[`pico-bay`](https://github.com/ctrlpi/pico-bay)**: Web app and MCP server for managing MicroPython and CircuitPython boards, such as the Raspberry Pi Pico and ESP32, over USB.
- **`pi-bay`** *(coming soon)*: A local web app and MCP server for Raspberry Pi boards over the network. Edit files, open a remote terminal, and manage your Pi remotely.

## License

[MIT](LICENSE)
