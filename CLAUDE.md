# CLAUDE.md - esp32-gpio-api

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A standalone **MicroPython** port of `pi-gpio-api` for ESP32 boards (classic ESP32, ESP32-S3,
ESP32-C3): the same REST API, the same wire format, the same behaviour, written against MicroPython
1.29 instead of FastAPI. A sibling of `pico-gpio-api`, from which it was ported.

It runs on **ESP32 hardware only**. There is no mock mode on the board: it imports `machine`,
`network` and `usocket` at the top. The unit tests fake those with `test_mocks/` (see Testing).

## Files on the board

| File | Role |
|---|---|
| `boot.py` | Starts the Wi-Fi connect early (reads `wifi.txt`) so the radio reserves its memory before the app loads |
| `main.py` | Three lines: `import app` |
| `app.mpy` | `app.py` precompiled by `build.sh` (`mpy-cross` 1.29), which saves the RAM of compiling ~1700 lines on the device |
| `config.json`, `config-backup.json`, `status.json`, `wifi.txt`, `scripts/` | Device state, all gitignored here |

**`app.py` is the source, `app.mpy` is what runs.** After any change to `app.py`, run `./build.sh`
and commit both: `app.mpy` is tracked, and `POST /upgrade` downloads it from GitHub `main`.

## Lockstep with `pi-gpio-api`

`api-tester` runs **one REST suite against every agent**, and `mcp-bridge`, `studio-bridge`,
`matter-bridge`, `homekit-bridge` and `homebridge-ctrlpi` treat a Pi, a Pico and an ESP32 alike. So
**a change to a route, a field name, a status code or a JSON shape here almost always needs the same
change in `pi-gpio-api` and `pico-gpio-api`.** State that in the plan before writing. Divergence is
only correct where the hardware forces it (pin tables, in-process sensor scripts, https support).
`VERSION` moves in lockstep with the other agents; never bump it unasked.

## The README is locked

`README.md` is done. Do not edit it without the user's explicit confirmation, even for a small wording or formatting fix, and even to keep it in sync with another project's README.

## Architecture (`app.py`, top to bottom)

| Section | What it holds |
|---|---|
| Constants | `VERSION`, `PORT` 8314, Wi-Fi/webhook timers, `UPGRADE_BASE_URL` + `UPGRADE_FILES` |
| Chip + pin tables | `PLATFORM` (from `os.uname().machine`), then `RESERVED_PINS` / `GPIO_PINS` / `INPUT_ONLY_PINS` per chip, `_validate_gpio()` |
| `_detect_default_led()` | Status LED per chip: C3 GPIO8 (active-low), S3 GPIO48 (WS2812 RGB), classic GPIO2 |
| `_normalize_config()` | The single place root aliases fold into their groups |
| Validators | `_norm_init`, `_is_script_name`, `_sensor_script_parts` |
| `Blinker` | Status LED, driven by `machine.Timer(0)`; on the S3 it writes `LED_RGB_COLOR` (dim green) through `neopixel` instead of a pin level |
| `WebhookSender` | Keep-alive HTTP(S) client for webhook POSTs |
| `GPIOManager` | Config/status persistence, pins, IRQ watches, sensors, logs, webhooks |
| Wi-Fi / time | `_read_wifi()`, `connect_wifi()`, `sync_time()`, `_gateway_alive()`, `WifiWatchdog`, then `_largest_free_block()` + `HeapWatch` (heap dead-man) |
| HTTP plumbing | `_recv_request()`, `_parse_request()`, `_send_all()`, `_json_pieces()`, `_send_json()`, `_send_empty()` |
| Upgrade | `_http_download()`, `_verify_download()`, `_do_upgrade()` |
| `handle_request()` | Always closes the socket (500 on `MemoryError`); `_handle_request()` is the route dispatch, one `elif` chain |
| `main()` | Boot, then the single-threaded accept loop |

### Pin tables (per chip)

`GPIO_PINS` is the only list `_validate_gpio()` accepts; anything else answers
`400 "GPIO<n> is unavailable or reserved on this ESP32"`. The chip is picked from
`os.uname().machine` (`"...ESP32-S3"`, `"...ESP32-C3"`, otherwise classic):

| Chip | Kept out (`RESERVED_PINS`) | Notes |
|---|---|---|
| Classic ESP32 | 1, 3 (UART0 REPL), 6-11 (SPI flash), 16, 17 (PSRAM) | 34-39 are input-only, no pulls. The firmware itself accepts 6-11 (verified on a DevKit V1), so this table is the only guard |
| ESP32-S3 | 19, 20 (native USB), 26-32 (flash/PSRAM), 33-37 (octal flash/PSRAM) | 22-25 do not exist; 43/44 (UART0) allowed |
| ESP32-C3 | 11 (VDD_SPI), 12-17 (flash), 18, 19 (native USB, the REPL) | 0-10, 20, 21 only |

On the S3 and C3 the UART0 pins are deliberately usable, because the tester rigs use them. Taking one
detaches a UART REPL until reset, and on a board with a USB-UART bridge the bridge holds both lines
HIGH. Note that `get_pin_info()` configures an unconfigured pin as an input with pull-up, so even a
`GET /gpio/read/<n>` claims the pin. The exception is the status LED pin (`_is_status_led()`): until
a client configures it, read and scan report it through the blinker's own `Pin` as an `output`,
because switching it to an input turns its driver off and the LED dies until reboot.

The unit tests mock a **classic** ESP32 and assert its table, so keep that branch unchanged unless the
tests change with it.

### Routes

`GET /hello` is answered **before** the key check; everything else needs `Api-Key`.

```
GET  /hello                     GET  /sensor/read
GET  /gpio/read                 GET  /sensor/read/{name}
GET  /gpio/read/{name|gpio}     POST /sensor/config/{name}
POST /gpio/write/{name|gpio}    GET  /config/read
POST /gpio/config/{name|gpio}   POST /config/update
GET  /gpio/watched              POST /config/load
GET  /gpio/scan                 POST /upgrade
GET  /logs                      POST /restart
```

An unknown route and an unknown pin/sensor both answer with an **empty body** (404), matching the Pi.

### Config

Grouped exactly as `pi-gpio-api` stores it: `gpios`, `notifications`, `bridges`, `settings`,
`sensors`, with `name` and `api_key` at the top level. `/config/read` adds a live `agent` block that
is **never stored**; `_READONLY_KEYS` drops it if a client sends it back. Root aliases
(`webhook_url`, `matter`, `log_days`, ...) fold into their groups via `_ROOT_ALIASES` and
`_normalize_config()`; change the table, not the call sites. On load, a stored pin outside the chip's
`GPIO_PINS` is dropped with a log line.

- **Text fields stay text**: a number sent for a key, name, URL or script (`api_key`, `name`, `notifications.*`, `bridges.*`, a pin's `name`, a sensor's `script`) is stored as text, so `4242` becomes `"4242"` and a key typed as digits never locks the agent out. `_as_text()` / `_text_fields()` run in `_normalize_config()` (every `/config/update` and `/config/load`) and in `config_pin()` / `config_sensor()`. `app.mpy` is rebuilt from it with `./build.sh`. `test_numbers_stored_as_text` covers it.

## MicroPython constraints that shaped the code

- **Single-threaded.** One accept loop serves one connection at a time. Nothing may sleep: pulse
  reverts are a `_pending_reverts` list polled from the loop, webhook retries a queue with due-times.
- **Heap fragmentation.** `WebhookSender` caches **https** sockets and reuses them, and sends plain
  http over throwaway sockets so a cheap send cannot evict the expensive TLS connection.
- **Flash wears out.** Config and status writes are deferred and coalesced (`_config_dirty`,
  `maybe_flush`, `_flush_all`). Anything that reboots must call `_flush_all()` first.
- **Wi-Fi drops silently.** A dead-man switch resets after `WIFI_DEAD_SECS` with the link
  reported down. A link that reports up but passes nothing is `WifiWatchdog`'s: only while idle (no
  request for `WIFI_PROBE_SECS`, and no pulse pending) it probes the gateway every
  `WIFI_PROBE_SECS`; two failures trigger a non-blocking reconnect (GPIO untouched), and the board
  resets only if the link is still dead `WIFI_RECONNECT_SECS` later. It logs `[WIFI] ...` lines.
- **System RAM is short on a classic ESP32.** About 24 KB of ESP-IDF heap is free after boot, and
  MicroPython can grow its own heap into it. When lwIP runs out, a blocking `sendall` never returns,
  so every reply goes through `_send_all()` (non-blocking, 3 s deadline), and request bodies are read
  into one `bytearray` sized from `Content-Length` rather than a bytes object regrown per chunk.
  Replies are never built whole: `_json_pieces()` yields the JSON (byte-for-byte `ujson.dumps`),
  `_send_json()` sizes it for an exact `Content-Length`, then sends it in ~`SEND_CHUNK` (512) byte
  writes. A whole-string `/gpio/scan` reply (~2 KB, plus its bytes copy) was what a classic short of
  system RAM could not send (`api-tester` test 17). If the system heap still runs out, `HeapWatch`
  (the heap dead-man) resets the board: the largest free block under `HEAP_LOW_BYTES` (6 KB) for
  `HEAP_LOW_SECS` (30 s), checked every `HEAP_CHECK_SECS`, with no pulse pending; it logs `[HEAP]
  ...` lines. A healthy classic shows 17-23 KB; the hung state measured 3.7 KB.
- **Request bodies** are capped at `MAX_REQUEST_BYTES` (8 KB): an oversized body is refused as
  `400` and the socket closed with the rest unread. It used to be drained (up to 64 KB), which filled
  lwIP's buffers and took a classic off the network at `api-tester` test 54; a client may now see a
  reset instead of the `400`, which the tester accepts. A chunked body (`Transfer-Encoding: chunked`, no
  `Content-Length`) is refused the same way rather than decoded, since otherwise its unread body
  would reach `/config/load` as `{}`, a full config reset. Same as `pico-gpio-api`; the Pi (`413`, chunked
  decoded) differs on purpose, and `api-tester` accepts both.
- **No `sh`.** Sensor scripts are `.py` files exec'd in-process with args going to `read(*args)`.
  Same validation as the Pi: a bare filename in `scripts/`, letters/digits args only.
- **https webhooks: S3 and C3 only** (`HTTPS_WEBHOOK_OK`, `_IS_CLASSIC`). A TLS handshake needs
  roughly 35 KB of contiguous system RAM; a classic ESP32 has about 24 KB free after boot (largest
  block ~19 KB), and every https POST failed with `OSError(12)` (ENOMEM) on the test rig. So, like
  the original Pico W, a classic answers `400` to an https `webhook_url` on `/config/update` and
  stores nothing, and `/config/load` drops one with a `[Config]` log line. A build without the
  `ssl` module refuses https the same way on every chip. `api-tester` tests a classic like a Pico
  W: webhook tests 33/34 use its local http listener and 20 asserts the refusal.
  `test_https_webhook_refused_on_classic` covers it (the unit-test mocks are a classic).

## Working on a board

Runs on the **Mac**, over USB serial via `mpremote` (installed into a local `venv/` on first use).

```bash
./build.sh                # app.py -> app.mpy (install.sh also runs it when app.py is newer)
./install.sh              # deploy boot.py + app.mpy + main.py to every ESP32 on USB
./install.sh --config     # force re-prompting for the API key / agent name
./install.sh --wifi       # force re-prompting for SSID / password
./install.sh --upgrade    # re-download the release files from GitHub first
```

`install.sh` finds boards by asking each serial port for `os.uname().sysname == "esp32"`, inspects
**the device** before prompting, and never overwrites an existing `config.json` / `wifi.txt` on it.
It seeds `scripts/cpu-temp.py` inline when the device has none. A single file can be pushed with
`venv/bin/mpremote connect <port> fs cp app.mpy :app.mpy + reset`.

Known quirk: on the C3 test board (built-in USB-Serial-JTAG, `303a:1001`), host-to-board serial works
but nothing comes back, so `mpremote` cannot enter the raw REPL and `install.sh` does not find it. To
reset it, pulse RTS with **DTR held low**; opening the port with pyserial's default DTR/RTS levels can
leave it in ROM download mode. The S3 test board sits behind a CH343 bridge (`1a86:55d3`), where
`mpremote` works normally.

`POST /upgrade` is the over-the-air path: it downloads `app.mpy`, `boot.py` and `main.py` from
`UPGRADE_BASE_URL`, verifies each one, swaps them in with `.backup` copies, then resets.

## Testing

```bash
python3 test.py           # unit tests: runs app.py under CPython with test_mocks/ (CI runs this)
cd ../api-tester && venv/bin/python run_tests.py <esp32-ip>   # full REST suite on hardware
```

The REST runner **remaps the suite's logical pins** (26, 19, 13, 6) to each rig's physical pins:
C3 `21, 20, 7, 6`, S3 `1, 2, 41, 42`, classic `23, 22, 19, 18` (verified on a DevKit V1). `--map A>B` overrides one. The loopback jumper joins the last two (C3 6↔7, S3 42↔41,
classic 18↔19); without it the nine loopback tests skip. Keep the S3 map off GPIO43/44: on a board
with a USB-UART bridge those lines are held HIGH, which fails test 55 (pull-down after drive).

## LED blink codes

| Pattern | Meaning |
|---|---|
| Fast 0.1 s | Booting: until Wi-Fi is up and the boot webhook is away |
| 1 blink | Flush check from the main loop (alive and idle) |
| 2 blinks | An HTTP request arrived |

## Open items (future tasks)

Found while testing, not yet done. Each needs the user's go-ahead before it is started.

- **System heap in the agent block.** Report `esp32.idf_heap_info(esp32.HEAP_DATA)` (total free,
  largest free block, low-water mark) in `/config/read`. Measured so far with a throwaway sensor
  script (`api-tester/ESP32-AGENT-NOTES.md`). The agent block is shared with `pi-gpio-api` and
  `pico-gpio-api`, so decide first whether this is an ESP32-only field.
- **Short-lived connection state.** Bursts of requests and webhook deliveries take up to ~5 KB of
  system RAM on a classic for about 120 s (lwIP TIME_WAIT: the agent closes every connection
  first); it all comes back. Measured, not a leak; no change made.
- **A setting for the status LED.** The LED pin is fixed per chip (`_detect_default_led()`). A
  `settings.status_led` (a pin number, or `false` to turn it off and free the pin) would let a
  user move or disable it. Open question: whether `pi-gpio-api` and `pico-gpio-api` accept it too.
- **Reserve GPIO43/44 on the S3.** Nothing needs them: the tester's S3 map is `1, 2, 41, 42`.
  On a board with a USB-UART bridge (CH343), any `/gpio/scan`
  takes them over and cuts the UART REPL until a reset, which blocks `mpremote`.
