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
| `main.py` | Imports `app`, or `upgrader` while `upgrade.lock` exists |
| `app.mpy` | `app.py` precompiled by `build.sh` (`mpy-cross` 1.29), which saves the RAM of compiling ~1700 lines on the device |
| `upgrader.mpy` | `upgrader.py` precompiled the same way: the over-the-air upgrade, run in its own boot (see below) |
| `config.json`, `config-backup.json`, `status.json`, `wifi.txt`, `scripts/` | Device state, all gitignored here |
| `upgrade.lock`, `upgrade-result.json`, `*.backup` | Left by `/upgrade` (see below) |

**`app.py` and `upgrader.py` are the source, the `.mpy` files are what runs.** After any change, run
`./build.sh` (it builds both) and commit the sources and the `.mpy` files: they are tracked, and
`POST /upgrade` downloads them from GitHub `main`.

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
| Constants | `VERSION`, `_PORT` 8314, Wi-Fi/webhook timers, `UPGRADE_BASE_URL` + `UPGRADE_FILES` |
| Chip + pin tables | `PLATFORM` (from `os.uname().machine`), then `RESERVED_PINS` / `GPIO_PINS` / `INPUT_ONLY_PINS` per chip, `_validate_gpio()` |
| `_detect_default_led()` | Status LED per chip: C3 GPIO8 (active-low), S3 GPIO48 (WS2812 RGB), classic GPIO2 |
| `_normalize_config()` | The single place root aliases fold into their groups |
| Validators | `_norm_init`, `_is_script_name`, `_sensor_script_parts` |
| `Blinker` | Status LED, driven by `machine.Timer(0)`; on the S3 it writes `LED_RGB_COLOR` (dim green) through `neopixel` instead of a pin level |
| `WebhookSender` | Keep-alive HTTP(S) client for webhook POSTs |
| `GPIOManager` | Config/status persistence, pins, IRQ watches, sensors, logs, webhooks |
| Wi-Fi / time | `_read_wifi()`, `connect_wifi()`, `sync_time()`, `_retry_ntp()`, `_gateway_alive()`, `WifiWatchdog`, then `_largest_free_block()` + `HeapWatch` (heap dead-man) |
| HTTP plumbing | `_recv_request()`, `_parse_request()`, `_send_all()`, `_send_json()`, `_send_empty()` |
| Upgrade | `_rm()`, `_do_upgrade()` (writes `upgrade.lock`), `_log_upgrade_result()`; the download itself is in `upgrader.py` |
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

`GET /hello` is answered **before** the key check; everything else needs `Api-Key`. A usable key is non-empty text (`_valid_key()`; a number counts, as text). An empty, null or non-text `api_key` counts as no key: `config.json` then falls back to the default, `/config/load` keeps the current key, and `/config/update` ignores it. An empty or missing `Api-Key` header is always refused. `test_bad_api_key_*` cover it.

```
GET  /hello                     GET  /sensor/read
GET  /gpio/read                 GET  /sensor/read/{name}
GET  /gpio/read/{name|gpio}     POST /sensor/config/{name}
POST /gpio/write/{name|gpio}    GET  /config/read
POST /gpio/config/{name|gpio}   POST /config/update
GET  /gpio/watched              POST /config/load
GET  /gpio/scan                 POST /upgrade
GET  /logs                      POST /restart
GET  /script/list               POST /script/write/{name}
GET  /script/read/{name}        POST /script/run/{name}
```

An unknown route and an unknown pin/sensor both answer with an **empty body** (404), matching the Pi.

### Config

Grouped exactly as `pi-gpio-api` stores it: `gpios`, `notifications`, `bridges`, `settings`,
`sensors`, with `name` and `api_key` at the top level. `/config/read` adds a live `agent` block that
is **never stored**; `_READONLY_KEYS` drops it if a client sends it back. Within it, what can't change
while the agent runs is read once (`os`, `serial`, `mac`, and the SSID once per connection), and
`config_updated` is set by `_config_stamp()` whenever the agent writes `config.json` instead of a stat
per read; `ip`, `signal`, `cpu_temperature` and `uptime` stay live (`TestAgentBlockCache`). Root aliases
(`webhook_url`, `matter`, `log_days`, ...) fold into their groups via `_ROOT_ALIASES` and
`_normalize_config()`; change the table, not the call sites. On load, a stored pin outside the chip's
`GPIO_PINS` is dropped with a log line.

- **Text fields stay text**: a number sent for a key, name, URL or script (`api_key`, `name`, `notifications.*`, `bridges.*`, a pin's `name`, a sensor's `script`) is stored as text, so `4242` becomes `"4242"` and a key typed as digits never locks the agent out. `_as_text()` / `_text_fields()` run in `_normalize_config()` (every `/config/update` and `/config/load`) and in `config_pin()` / `config_sensor()`. `app.mpy` is rebuilt from it with `./build.sh`. `test_numbers_stored_as_text` covers it.

## MicroPython constraints that shaped the code

- **The clock.** NTP is tried at boot, then by `_retry_ntp()` every `_NTP_RETRY_MS` (2 min) from the main loop while the clock reads before 2023: one lost answer on a weak link used to leave the clock at 2000 until the next reboot, with every log line stamped 2000-01-01 and `config_updated` empty (`api-tester` test 67 then skips). On success the clock jumps, so `_BOOT_TIME` moves by the same amount and `uptime` stays right; it logs `[BOOT] Clock set over NTP`. `TestNtpRetry` covers it.
- **Single-threaded.** One accept loop serves one connection at a time. Nothing may sleep: pulse
  reverts are a `_pending_reverts` list polled from the loop, webhook retries a queue with due-times.
- **Heap fragmentation.** `WebhookSender` caches **https** sockets and reuses them, and sends plain
  http over throwaway sockets so a cheap send cannot evict the expensive TLS connection.
- **A webhook never holds the server long.** The server and the webhooks share one thread, and a
  C3 was measured stuck 37 s in one https delivery while every request timed out. So each host is
  looked up once (`WebhookSender._addrs`; `getaddrinfo` has no timeout) and again only after a failed
  connect, connect and TLS reads time out after 3 s, and only a *reused* kept-alive socket gets an
  immediate second try (the server may have closed it while idle, which fails fast). A fresh
  connection that fails is left to the retry queue (`_WEBHOOK_RETRY_MS`, 30 s later).
  `TestWebhookSender` covers it.
- **Every reset the agent makes leaves a note.** `_reset(reason, soft=False)` writes
  `last-reset.txt` (time and reason) before resetting: the Wi-Fi connect failure, `WifiWatchdog`,
  `HeapWatch`, the Wi-Fi dead-man, port 8314 in use, `/restart` with `reboot`, and `/upgrade`. The
  log is in memory and lost with the reset, so at the next boot `_log_reset_cause()` logs
  `[BOOT] Reset cause: <machine.reset_cause()>; the agent reset itself at <note>` and deletes the
  note. `TestResetNote` covers it.
- **Flash wears out.** Config and status writes are deferred and coalesced (`_config_dirty`,
  `maybe_flush`, `_flush_all`). Anything that reboots must call `_flush_all()` first.
- **Wi-Fi drops silently.** A dead-man switch resets after `_WIFI_DEAD_SECS` with the link
  reported down. A link that reports up but passes nothing is `WifiWatchdog`'s: only while idle (no
  request for `_WIFI_PROBE_SECS`, and no pulse pending) it probes the gateway every
  `_WIFI_PROBE_SECS`; two failures trigger a non-blocking reconnect (GPIO untouched), and the board
  resets only if the link is still dead `_WIFI_RECONNECT_SECS` later. It logs `[WIFI] ...` lines.
- **System RAM is short on a classic ESP32.** About 24 KB of ESP-IDF heap is free after boot, and
  MicroPython can grow its own heap into it. When lwIP runs out, a blocking `sendall` never returns,
  so every reply goes through `_send_all()` (non-blocking, 3 s deadline), and request bodies are read
  into one `bytearray` sized from `Content-Length` rather than a bytes object regrown per chunk.
  A reply goes out as one `ujson.dumps` in one write with its header. It used to be built in pieces
  (`_json_pieces()`) and sent in ~512-byte writes, after a whole-string `/gpio/scan` reply failed on a
  classic short of system RAM (`api-tester` test 17); but on an S3 the pieces made a big reply take
  about twice as long and a full `api-tester` run 214 s against 138 s, so every chip is back to one
  write. If the system heap still runs out, `HeapWatch`
  (the heap dead-man) resets the board: the largest free block under `_HEAP_LOW_BYTES` (6 KB) for
  `_HEAP_LOW_SECS` (30 s), checked every `_HEAP_CHECK_SECS`, with no pulse pending, and never in the
  first `_HEAP_BOOT_GRACE_SECS` (180 s) after boot; it logs `[HEAP] ...` lines. A healthy classic shows
  17-23 KB; the hung state measured 3.7 KB. The grace is for the ESP32-C3, which measured 5.6-9.7 KB in
  the first minutes after boot and 20-24 KB once settled (never under 12 KB through a full
  `api-tester` run); a stretch that began in the grace still counts, so a board still low when it
  ends is reset then. `test_no_reset_in_the_first_minutes_after_boot` covers it.
- **Request bodies** are capped at `_MAX_REQUEST_BYTES` (8 KB): an oversized body is refused as
  `400` and the socket closed with the rest unread. It used to be drained (up to 64 KB), which filled
  lwIP's buffers and took a classic off the network at `api-tester` test 53; a client may now see a
  reset instead of the `400`, which the tester accepts. A chunked body (`Transfer-Encoding: chunked`, no
  `Content-Length`) is refused the same way rather than decoded, since otherwise its unread body
  would reach `/config/load` as `{}`, a full config reset. Same as `pico-gpio-api`; the Pi (`413`, chunked
  decoded) differs on purpose, and `api-tester` accepts both.
- **No `sh`.** Sensor scripts are `.py` files exec'd in-process with args going to `read(*args)`.
  `gc.collect()` runs right before each sensor read and script run: a script's own allocation
  (`'x' * 5000` in api-tester test 63) failed on a classic without it.
- **`gc.threshold(_GC_THRESHOLD)`** (16 KB), set at the start of `main()`: MicroPython collects
  after that much allocation instead of only when an allocation fails, since on an ESP32 a full
  heap grows into the system RAM the network stack needs.
  Same validation as the Pi: a bare filename in `scripts/`, letters/digits args only.
- **Scripts (`/script/*`).** `GPIOManager.list_scripts()` / `read_script()` / `write_script()` /
  `run_script()`, raising `ScriptError(status, message)`; the same routes, status codes and JSON as
  `pi-gpio-api` and `pico-gpio-api`, and the same code as the Pico. Names use `_is_script_name()` plus
  no leading `-` and `_SCRIPT_NAME_MAX` (32) in `_script_path()`; a subfolder is `400`. A run execs the
  file in-process like a sensor: `output` is `read(*args)` or `result` as text (JSON for a
  non-string), `exit_code` `1` with `"<Error>: <message>"` when it raises (`SystemExit` included).
  No timeout, unlike the Pi's `504`. Writes go through a hidden `.<name>.tmp` and `os.rename`, and
  create `scripts/` when it's missing. `write_script()` answers `403` while `api_key` is the default
  and while `scripts/write.lock` exists. A write may create `write.lock` (nothing else ever does,
  install included), and from then on every write, the lock's own included, is refused, so
  `/script/write` can't lift it; USB can (`./install.sh --unlock`), and so can a script run through
  `/script/run`, since runs still work while locked (api-tester test 59 unlocks that way). `write.lock` in any case is never run. The `test_script_*`
  tests cover it.
- **https webhooks: S3 only** (`HTTPS_WEBHOOK_OK`, `_IS_S3`). A TLS handshake needs roughly 35 KB
  of contiguous system RAM; a classic ESP32 has about 24 KB free after boot (largest block ~19 KB),
  and every https POST failed with `OSError(12)` (ENOMEM) on the test rig. The C3 was marginal and
  then failed outright with the agent loaded (`MPI_ALLOC_FAILED`, `X509_ALLOC_FAILED`, even right
  after boot, with `hooks.ctrlpi.com` now sending a three-certificate chain with a 4096-bit RSA
  root), and its failed handshakes hung the board: without the https tests it passed 66/68, with
  them it hung. So, like the original Pico W, a classic and a C3 answer `400` to an https
  `webhook_url` on `/config/update` (the message names the board) and store nothing, and
  `/config/load` drops one with a `[Config]` log line. A build without the `ssl` module refuses
  https the same way on every chip. `api-tester` tests both like a Pico W: webhook tests 32/33 use
  its local http listener and 20 asserts the refusal. `test_https_webhook_refused_on_classic` and
  `TestHttpsByChip` cover it.

## Working on a board

Runs on the **Mac**, over USB serial via `mpremote` (installed into a local `venv/` on first use).

```bash
./build.sh                # app.py -> app.mpy, upgrader.py -> upgrader.mpy (install.sh runs it when a source is newer)
./install.sh              # deploy boot.py + app.mpy + upgrader.mpy + main.py to every ESP32 on USB
./install.sh --config     # force re-prompting for the API key / agent name
./install.sh --wifi       # force re-prompting for SSID / password
./install.sh --upgrade    # re-download the release files from GitHub first
./install.sh --unlock     # install nothing: delete scripts/write.lock on every ESP32, then reset it
```

`install.sh` finds boards by asking each serial port for `os.uname()`: it installs on `sysname`
`esp32` except an ESP32-S2 (no agent), and resets every other board that answered, since the
question stops its program. It inspects
**the device** before prompting, and never overwrites an existing `config.json` / `wifi.txt` on it.
It seeds `scripts/cpu-temp.py` inline when the device has none. A single file can be pushed with
`venv/bin/mpremote connect <port> fs cp app.mpy :app.mpy + reset`.

Known quirk: on the C3 test board (built-in USB-Serial-JTAG, `303a:1001`), host-to-board serial works
but nothing comes back, so `mpremote` cannot enter the raw REPL and `install.sh` does not find it. To
reset it, pulse RTS with **DTR held low**; opening the port with pyserial's default DTR/RTS levels can
leave it in ROM download mode. The S3 test board sits behind a CH343 bridge (`1a86:55d3`), where
`mpremote` works normally.

`POST /upgrade` is the over-the-air path, in two boots. The agent writes `upgrade.lock` (JSON:
`url` = `UPGRADE_BASE_URL`, `files` = `UPGRADE_FILES`, `attempt`), answers `200 {"status": "upgrading"}`
and soft-resets; `main.py` sees the lock and imports `upgrader` instead of `app`, so the TLS download
runs on a fresh heap that a running agent no longer has (measured on the C3: 102 KB free, largest
block 94 KB, after a soft reset). `upgrader.mpy` downloads every file as `<name>.new`, verifies it,
swaps them in with `.backup` copies (rolled back on failure), writes `upgrade-result.json`, removes
the lock and soft-resets into the agent, which logs `[UPGRADE] Done, now <version>: ...` (or
`Failed, still ...`) once. A failed attempt keeps the lock and hard-resets (a fresh radio) for the
next one; the third failure gives up and the old agent starts. Soft resets keep the Wi-Fi link: with
hard resets the Pico 2 W test board was unreachable for ~90 s after the second quick reassociation,
and an upgrade took 43-123 s instead of ~10 s. A board without `upgrader.mpy` (installed before
this) answers `500` and moves over once by USB; `install.sh` also deletes a leftover `upgrade.lock`,
which would otherwise boot the upgrader over the files it just installed. The upgrader takes the URL
and file list from the lock, so both are set in `app.py` only. `TestUpgrade` runs `upgrader.py`
against a local release folder.

## Testing

```bash
python3 test.py           # unit tests: runs app.py under CPython with test_mocks/ (CI runs this)
cd ../api-tester && venv/bin/python run_tests.py <esp32-ip>   # full REST suite on hardware
```

The REST runner **remaps the suite's logical pins** (26, 19, 13, 6) to each rig's physical pins:
C3 `21, 20, 7, 6`, S3 `1, 2, 41, 42`, classic `23, 22, 19, 18` (verified on a DevKit V1). `--map A>B` overrides one. The loopback jumper joins the last two (C3 6↔7, S3 42↔41,
classic 18↔19); without it the nine loopback tests skip. Keep the S3 map off GPIO43/44: on a board
with a USB-UART bridge those lines are held HIGH, which fails test 54 (pull-down after drive).

## LED blink codes

| Pattern | Meaning |
|---|---|
| Fast 0.1 s | Booting: until Wi-Fi is up and the server is listening (it used to wait for the boot webhook, which kept it blinking for a minute or more when a target was unreachable) |
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
