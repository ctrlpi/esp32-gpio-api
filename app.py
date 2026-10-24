# ESP32 GPIO REST API

import gc
import machine
import micropython
import network
import os
import usocket
import ujson
import utime

try:
	import uerrno
except ImportError:
	import errno as uerrno

try:
	import ssl
	_HAS_SSL = True
except ImportError:
	_HAS_SSL = False

# Constants

# Used only when wifi.txt has no SSID / PASSWORD.
FALLBACK_SSID     = "MyNetwork"
FALLBACK_PASSWORD = "MyPassword"
DEFAULT_API_KEY   = "your-secret-key"
VERSION           = "0.9.27"  # API version
DEFAULT_LOG_DAYS  = 7
PORT              = 8314
WIFI_DEAD_SECS    = 40    # dead-man switch: reset if WiFi stays lost this long
WIFI_PROBE_SECS   = 120   # idle this long: probe the gateway, then this often
WIFI_RECONNECT_SECS = 20  # a soft reconnect gets this long before the board resets
HEAP_LOW_BYTES    = 6144  # heap dead-man: largest free system-RAM block below this...
HEAP_LOW_SECS     = 30    # ...for this long (no pulse pending) flushes and resets
HEAP_CHECK_SECS   = 5     # how often the main loop looks
WEBHOOK_MIN_MS    = 200   # watch debounce
WEBHOOK_IDLE_MS   = 90000 # socket idle timeout
WEBHOOK_RETRIES   = 2     # extra attempts
WEBHOOK_RETRY_MS  = 30000 # retry delay
# Files on flash: the config, its last good copy (read when config.json fails to
# parse) and each pin's last logical level (restored at boot for init "last").
CONFIG_FILE       = "config.json"
BACKUP_FILE       = "config-backup.json"
STATUS_FILE       = "status.json"
# POST /upgrade downloads UPGRADE_FILES from here: (name, kind, minimum size in bytes).
UPGRADE_BASE_URL  = "https://raw.githubusercontent.com/ctrlpi/esp32-gpio-api/main/"
UPGRADE_FILES     = (("app.mpy", "mpy", 2000),
                     ("boot.py", "py", 200),
                     ("main.py", "py", 20))
# The log is an in-memory ring of LOG_MAX lines; /logs returns the newest LOG_RETURN.
LOG_MAX           = 50
LOG_RETURN        = 30
# Largest request accepted, headers and body together; a bigger one is refused (400).
MAX_REQUEST_BYTES = 8192

# The board string, e.g. "Generic ESP32S3 module with ESP32-S3"; it picks the
# pin tables and the status LED.
try:
	_MACHINE = os.uname().machine
except Exception:
	_MACHINE = "ESP32"
PLATFORM = _MACHINE or "ESP32"

# Pin tables per chip. Protect flash, REPL and PSRAM.
_CHIP = PLATFORM.lower()
if "s3" in _CHIP:
	# 19/20 native USB, 26-32 SPI flash/PSRAM, 33-37 octal flash/PSRAM.
	# UART0 (43/44) stays usable: taking it detaches a UART REPL until reset.
	RESERVED_PINS   = (19, 20, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37)
	GPIO_PINS       = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18,
	                   21, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48)
	INPUT_ONLY_PINS = ()
elif "c3" in _CHIP:
	# 11 VDD_SPI, 12-17 SPI flash, 18/19 native USB (the REPL).
	# UART0 (20/21) stays usable: taking it detaches a UART REPL until reset.
	RESERVED_PINS   = (11, 12, 13, 14, 15, 16, 17, 18, 19)
	GPIO_PINS       = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 21)
	INPUT_ONLY_PINS = ()
else:
	# 1/3 UART0 REPL, 6-11 SPI flash, 16/17 PSRAM on WROVER modules.
	RESERVED_PINS   = (1, 3, 6, 7, 8, 9, 10, 11, 16, 17)
	GPIO_PINS       = (0, 2, 4, 5, 12, 13, 14, 15, 18, 19, 21, 22, 23,
	                   25, 26, 27, 32, 33, 34, 35, 36, 37, 38, 39)
	INPUT_ONLY_PINS = (34, 35, 36, 37, 38, 39)

# Raises ValueError for a pin outside this chip's GPIO_PINS, and for an output or a
# pull resistor on an input-only pin.
def _validate_gpio(gpio, conf=None):
	if gpio not in GPIO_PINS:
		raise ValueError("GPIO%d is unavailable or reserved on this ESP32" % gpio)
	if conf is not None and gpio in INPUT_ONLY_PINS:
		if conf.get("type", "input") != "input":
			raise ValueError("GPIO%d is input-only" % gpio)
		if conf.get("pullup", "none") != "none":
			raise ValueError("GPIO%d has no internal pull resistors" % gpio)


# The status LED for this chip: (pin, active_low, is_rgb).
def _detect_default_led():
	m = (PLATFORM or "").lower()
	if "c3" in m:
		return 8, True, False   # ESP32-C3: GPIO 8 (active-low on SuperMini/LuatOS)
	elif "s3" in m:
		return 48, False, True  # ESP32-S3: GPIO 48 (WS2812 RGB on DevKitC-1 style boards)
	else:
		return 2, False, False  # Generic ESP32: GPIO 2 (active-high blue LED)

LED_PIN, LED_ACTIVE_LOW, LED_RGB = _detect_default_led()
LED_RGB_COLOR           = (0, 40, 0)  # dim green; a WS2812 at full power is blinding
LED_TIMER_ID            = 0

# An https webhook needs the ssl module, and a TLS handshake needs roughly 35 KB of
# contiguous system RAM. A classic ESP32 has about 24 KB free after boot (largest block
# ~19 KB), so every https POST fails with ENOMEM: like the original Pico W, it refuses
# https URLs instead of storing one that never delivers. The S3 and C3 have the room.
_IS_CLASSIC         = "s3" not in _CHIP and "c3" not in _CHIP
HTTPS_WEBHOOK_OK    = _HAS_SSL and not _IS_CLASSIC
HTTPS_WEBHOOK_ERROR = ("https webhook URLs are not supported on a classic ESP32: TLS needs more "
                       "memory than it has free. Use an http:// URL, or an ESP32-S3 or C3."
                       if _IS_CLASSIC else
                       "https webhook URLs require an SSL-enabled MicroPython build")

def _is_https(url):
	return str(url or "").strip().lower().startswith("https://")

# Reason phrases for the status codes the agent sends.
_STATUS_TEXT = {
	200: "OK", 400: "Bad Request", 403: "Forbidden",
	404: "Not Found", 422: "Unprocessable Entity", 500: "Internal Server Error",
}

# Sensors run scripts from this folder only (see _sensor_script_parts).
SCRIPTS_DIR = "scripts"
# Keys a client may see but never set. `agent` is computed live on every read
# (platform/os/ip/cpu_temperature/version), so it is dropped from whatever comes in
# on /config/update and /config/load rather than being written to config.json - a
# read-modify-load round trip would otherwise persist a stale snapshot of it.
_READONLY_KEYS = ("agent",)

# Root-level (ungrouped) spelling -> (group, field). A client may send either form on
# /config/update and /config/load; _normalize_config() routes the root form into its
# group so config.json is always written in the canonical grouped shape. The webhook
# pair keeps its familiar `webhook_*` names rather than a bare `url`/`key`, which would
# be ambiguous next to the agent's own api_key.
_ROOT_ALIASES = {
	"webhook_url":  ("notifications", "webhook"),
	"webhook_key":  ("notifications", "webhook_key"),
	"logs_enabled": ("settings", "logs_enabled"),
	"log_days":     ("settings", "log_days"),
	"matter":       ("bridges", "matter"),
	"matter_key":   ("bridges", "matter_key"),
	"homekit":      ("bridges", "homekit"),
	"homekit_key":  ("bridges", "homekit_key"),
	"homebridge":     ("bridges", "homebridge"),
	"homebridge_key": ("bridges", "homebridge_key"),
}

# Drops read-only keys and moves root-level aliases into their groups; a field
# also sent inside its group wins.
def _normalize_config(data):
	out = {k: v for k, v in data.items() if k not in _READONLY_KEYS and k not in _ROOT_ALIASES}
	for alias in _ROOT_ALIASES:
		if data.get(alias) is None:
			continue
		group, field = _ROOT_ALIASES[alias]
		target = out.setdefault(group, {})
		if isinstance(target, dict) and field not in target:
			target[field] = data[alias]
	return _text_fields(out)

# Keys, names, URLs and scripts are text: a number sent for one is stored as text
# (4242 -> "4242"), so a key typed as digits never locks the agent.
def _as_text(v):
	return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

def _text_fields(out):
	for k in ("name", "api_key"):
		if k in out:
			out[k] = _as_text(out[k])
	for group in ("notifications", "bridges"):
		if isinstance(out.get(group), dict):
			out[group] = {f: _as_text(v) for f, v in out[group].items()}
	for section, field in (("gpios", "name"), ("sensors", "script")):
		entries = out.get(section)
		for entry in (entries.values() if isinstance(entries, dict) else ()):
			if isinstance(entry, dict) and field in entry:
				entry[field] = _as_text(entry[field])
	return out

# A pin's stored init: 0, 1, or "last" (the level saved in status.json).
INIT_VALUES = (0, 1, "last")

def _norm_init(v):
	# Word spellings are folded to the canonical form here, before the type is
	# checked, so config.json only ever holds 0/1/"last" and /config/read only ever
	# answers those - "on"/"off"/"restore" are input conveniences, not new states.
	if isinstance(v, str):
		low = v.lower()
		if low == "none":    return None
		if low == "on":      return 1
		if low == "off":     return 0
		if low == "restore": return "last"
		return low
	if v is True:  return 1
	if v is False: return 0
	if isinstance(v, float):
		if v != int(v):
			return v
		v = int(v)
	if isinstance(v, int):
		return 1 if v != 0 else 0
	return v

# Raises ValueError unless v (already normalised) is None or one of INIT_VALUES.
def _check_init(v):
	if v is not None and v not in INIT_VALUES:
		raise ValueError('init must be 0, 1 or "last" (or "on"/"off"/"restore"/"none"), got ' + repr(v))
	return v

# --- Sensor script validation ---
# A sensor runs only a bare filename from the scripts/ folder, with letters/digits
# arguments, so an API-key holder can't run or read anything else. Checked when a
# sensor is configured (400) and again on every read ("blocked: <reason>"), since
# /config/load and a hand-edited config.json skip /sensor/config.
def _is_alnum(s):
	return bool(s) and all(("0" <= c <= "9") or ("a" <= c <= "z") or ("A" <= c <= "Z") for c in s)

def _is_script_name(s):
	if not s or s[0] == ".":
		return False
	return all(_is_alnum(c) or c in "._-" for c in s)

# Returns (error, path, args): an error message, or the script's path in scripts/
# and its arguments.
def _sensor_script_parts(script):
	parts = (script or "").split()
	if not parts:
		return "empty script", None, None
	if not _is_script_name(parts[0]):
		return "script must be a bare filename in the scripts folder", None, None
	for a in parts[1:]:
		if not _is_alnum(a):
			return "script args must be letters and digits only", None, None
	return None, SCRIPTS_DIR + "/" + parts[0], parts[1:]

# LED blinker

class Blinker:
	"""The status LED: fast blinking while booting, then short blinks run by a timer.
	On the S3 it lights the WS2812 RGB LED through neopixel."""

	def __init__(self):
		self._led = None
		self._np = None
		self._timer = None
		self._active_low = LED_ACTIVE_LOW
		if LED_PIN is not None:
			try:
				_validate_gpio(LED_PIN, {"type": "output"})
				init_val = 1 if self._active_low else 0
				self._led = machine.Pin(LED_PIN, machine.Pin.OUT, value=init_val)
				if LED_RGB:
					import neopixel
					self._np = neopixel.NeoPixel(self._led, 1)
					self._np.write()
				self._timer = machine.Timer(LED_TIMER_ID)
			except Exception:
				self._led = None
				self._np = None
		self._on    = 0
		self._left  = 0
		self._fast  = False

	# v is the logical state (1 = lit); handles active-low and RGB LEDs.
	def _set(self, v):
		self._on = v
		if self._np:
			self._np[0] = LED_RGB_COLOR if v else (0, 0, 0)
			self._np.write()
		elif self._led:
			pin_val = (1 - v) if self._active_low else v
			self._led.value(pin_val)

	# Toggles every 100 ms until stop_fast(); used while booting.
	def start_fast(self):
		if not self._led:
			return
		self._fast = True
		self._timer.init(period=100, mode=machine.Timer.PERIODIC, callback=lambda t: self._set(0 if self._on else 1))

	def stop_fast(self):
		if not self._led or not self._fast:
			return
		self._fast = False
		self._timer.deinit()
		self._set(0)

	# Blinks count times, 200 ms on and 200 ms off, on a timer so nothing sleeps. A
	# blink already running is extended, never restarted.
	def blink(self, count):
		if not self._led or self._fast:
			return
		if self._left > 0:
			if count > self._left:
				self._left = count
			return
		self._left = count
		self._set(1)
		self._timer.init(period=200, mode=machine.Timer.PERIODIC, callback=self._step)

	# Timer callback for blink().
	def _step(self, t):
		if self._on:
			self._set(0)
			self._left -= 1
			if self._left <= 0:
				self._timer.deinit()
		elif self._left > 0:
			self._set(1)

BLINKER = Blinker()

# Webhooks

class WebhookSender:
	"""Sends webhook POSTs. An https connection is kept open and reused, since a fresh
	TLS handshake can fail for lack of contiguous memory; plain http uses a new socket
	per POST, so a cheap send never evicts the expensive connection."""

	def __init__(self):
		self._sock = None
		self._url  = None
		self._last = 0

	def _close(self):
		if self._sock:
			try:
				self._sock.close()
			except Exception:
				pass
		self._sock = None

	# Opens a socket (TLS for https, certificate not verified); returns (socket, host, path).
	def _connect(self, url):
		proto, rest = url.split("://", 1)
		host_port, _, path = rest.partition("/")
		host, _, port = host_port.partition(":")
		port = int(port) if port else (443 if proto == "https" else 80)
		if proto == "https" and not _HAS_SSL:
			raise OSError("no ssl module")
		gc.collect()
		s = usocket.socket()
		s.settimeout(3)
		try:
			s.connect(usocket.getaddrinfo(host, port)[0][-1])
			if proto == "https":
				gc.collect()
				ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
				ctx.verify_mode = ssl.CERT_NONE
				s = ctx.wrap_socket(s, server_hostname=host)
			return s, host, "/" + path
		except Exception:
			try:
				s.close()
			except Exception:
				pass
			raise

	# True when the cached https socket has been idle longer than WEBHOOK_IDLE_MS.
	def _expired(self):
		return self._sock is not None and utime.ticks_diff(utime.ticks_ms(), self._last) > WEBHOOK_IDLE_MS

	# Called from the main loop: closes an idle cached socket.
	def maybe_expire(self):
		if self._expired():
			self._close()

	# Any status outside 2xx is a failed delivery, so it gets retried.
	@staticmethod
	def _check(status):
		if status >= 300:
			raise OSError(f"HTTP {status}")

	# One delivery. http uses a throwaway socket; https reuses the cached socket,
	# reconnecting once if it has gone stale. Raises on failure.
	def send(self, url, payload, api_key):
		body = ujson.dumps(payload).encode()
		if not _is_https(url):
			sock, host, path = self._connect(url)
			try:
				status, _ = self._request(sock, host, path, body, api_key)
			finally:
				try:
					sock.close()
				except Exception:
					pass
			self._check(status)
			return
		for attempt in (0, 1):
			try:
				if self._sock is None or self._url != url or self._expired():
					self._close()
					self._sock, self._host, self._path = self._connect(url)
					self._url = url
				status, keep = self._request(self._sock, self._host, self._path, body, api_key)
				if not keep:
					self._close()
				self._last = utime.ticks_ms()
			except Exception:
				self._close()
				if attempt:
					raise
				continue
			self._check(status)
			return

	# Writes the POST, then reads the status line, headers and body. Returns
	# (status, keep_alive).
	def _request(self, sock, host, path, body, api_key):
		s = sock
		s.write(("POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\n"
		         "Content-Length: {}\r\n{}Connection: keep-alive\r\n\r\n").format(
		             path, host, len(body),
		             "Api-Key: {}\r\n".format(api_key) if api_key else "").encode())
		s.write(body)
		line = s.readline()
		if not line or not line.startswith(b"HTTP/"):
			raise OSError("bad response")
		try:
			status = int(line.split(b" ")[1])
		except Exception:
			status = 0
		cl, keep = 0, True
		while True:
			line = s.readline()
			if not line or line == b"\r\n":
				break
			ll = line.lower()
			if ll.startswith(b"content-length:"):
				cl = int(line.split(b":", 1)[1])
			elif ll.startswith(b"connection:") and b"close" in ll:
				keep = False
			elif ll.startswith(b"transfer-encoding:"):
				keep = False
		while cl > 0:
			chunk = s.read(min(cl, 256))
			if not chunk:
				break
			cl -= len(chunk)
		return status, keep

# GPIO

class PinNotFound(Exception): pass
class SensorNotFound(PinNotFound): pass

class GPIOManager:
	"""Pins, config, sensors, logs and webhooks. Flash writes are deferred: a change
	marks the config or status dirty and maybe_flush() writes it."""

	def __init__(self):
		self.config      = {}
		self.status      = {}
		self.pins        = {}
		self.pin_configs = {}
		self.watches     = {}
		self._webhook_queue   = []
		self._webhook_sender  = WebhookSender()
		self._pending_reverts = []
		self._last_watch_ms   = {}
		self._log        = []
		self._last_log_cleanup = utime.time()
		self._config_dirty = False
		self._status_dirty = False
		self._last_flush   = utime.time()
		self._load_config()
		self._load_status()
		self._initialize_pins()
		self.log("[BOOT] GPIO manager ready")

	# Loads config.json (config-backup.json when it fails to parse), fills in defaults,
	# and drops what can't apply: pins outside this chip's table, a pullup on a
	# non-input, watched:false, and an invalid init.
	def _load_config(self):
		try:
			with open(CONFIG_FILE) as f:
				self.config = ujson.load(f)
		except OSError:
			self.config = {}
		except Exception as e:
			self.log(f"[Config] {CONFIG_FILE} is invalid ({e}); loading backup {BACKUP_FILE}")
			try:
				with open(BACKUP_FILE) as f:
					self.config = ujson.load(f)
			except Exception:
				self.config = {}
		self.config.setdefault("api_key",       DEFAULT_API_KEY)
		self.config.setdefault("gpios",         {})
		self.config.setdefault("notifications", {})
		self.config.setdefault("bridges",       {})
		self.config.setdefault("settings",      {})
		self.config.setdefault("sensors",       {})
		self.config["settings"].setdefault("logs_enabled", True)
		self.config["settings"].setdefault("log_days", DEFAULT_LOG_DAYS)
		try:
			self.config["settings"]["log_days"] = max(0, min(int(self.config["settings"]["log_days"]), 30))
		except (TypeError, ValueError):
			self.config["settings"]["log_days"] = DEFAULT_LOG_DAYS
		for g in list(self.config["gpios"]):
			if not g.isdigit() or int(g) not in GPIO_PINS:
				del self.config["gpios"][g]
				self.log(f"[Config] GP{g} dropped (unavailable or reserved on ESP32)")
				continue
			conf = self.config["gpios"][g]
			if conf.get("type") in ("output", "vcc", "gnd"):
				conf.pop("pullup", None)
			if conf.get("watched") is not True:
				conf.pop("watched", None)
			if "init" in conf:
				iv = _norm_init(conf["init"])
				if iv in INIT_VALUES:
					conf["init"] = iv
				else:
					conf.pop("init", None)
		self.pin_configs = self.config["gpios"]

	# Marks the config dirty; maybe_flush() writes it.
	def _save_config(self):
		self._config_dirty = True

	# Records each pin's logical value (what /gpio/read reports) for init "last",
	# and marks the status dirty.
	def _save_status(self):
		for gpio_str, pin in self.pins.items():
			conf = self.pin_configs.get(gpio_str, {})
			if conf.get("type") in ("output", "input"):
				reversed_flag         = conf.get("reversed", False)
				level                 = pin.value()
				logical               = (1 - level) if reversed_flag else level
				self.status[gpio_str] = logical
		self._status_dirty = True

	# Writes config.json if it changed; a failed write stays dirty and is retried.
	def _flush_config(self):
		if not self._config_dirty:
			return
		self._config_dirty = False
		try:
			with open(CONFIG_FILE, "w") as f:
				ujson.dump(self.config, f)
		except Exception:
			self._config_dirty = True

	def _flush_status(self):
		if not self._status_dirty:
			return
		self._status_dirty = False
		try:
			with open(STATUS_FILE, "w") as f:
				ujson.dump(self.status, f)
		except Exception:
			self._status_dirty = True

	# Anything that resets the board calls this first.
	def _flush_all(self):
		self._flush_config()
		self._flush_status()

	# Called from the main loop: writes pending changes at most every 2 s, so a burst
	# of changes is one flash write. Its single LED blink is the idle heartbeat.
	def maybe_flush(self):
		if utime.time() - self._last_flush < 2:
			return
		self._last_flush = utime.time()
		BLINKER.blink(1)
		self._flush_all()

	# Copies config.json to config-backup.json before the config is replaced.
	def _backup_config(self):
		self._flush_config()
		try:
			with open(CONFIG_FILE) as src:
				data = src.read()
		except OSError:
			return
		with open(BACKUP_FILE, "w") as dst:
			dst.write(data)

	# Each pin's last saved logical level, for init "last".
	def _load_status(self):
		try:
			with open(STATUS_FILE) as f:
				self.status = ujson.load(f)
		except Exception:
			self.status = {}

	# Sets up every configured pin; one that fails is logged and skipped.
	def _initialize_pins(self):
		for gpio_str, conf in list(self.pin_configs.items()):
			try:
				self._init_pin(gpio_str, conf)
			except Exception as e:
				self.log(f"[GPIO] Init GP{gpio_str} failed: {type(e).__name__} {repr(e)}")

	# Sets up one pin per its type, init and watch settings. An output starts at
	# init 0/1, at its saved level for init "last" or when it already was an output
	# (was_output), else LOW. A watched input with an init fires a webhook at once
	# when it doesn't read the expected value.
	def _init_pin(self, gpio_str, conf, was_output=False):
		gpio          = int(gpio_str)
		_validate_gpio(gpio, conf)
		pin_type      = conf.get("type", "input")
		reversed_flag = conf.get("reversed", False)

		existing = self.pins.get(gpio_str)
		if existing:
			try:
				existing.irq(handler=None)
			except Exception:
				pass
		self.watches.pop(gpio_str, None)

		if pin_type == "vcc":
			p = machine.Pin(gpio, machine.Pin.OUT)
			p.value(1)
		elif pin_type == "gnd":
			p = machine.Pin(gpio, machine.Pin.OUT)
			p.value(0)
		elif pin_type == "output":
			p    = machine.Pin(gpio, machine.Pin.OUT)
			init = conf.get("init")
			if init == "last":
				last_logical = self.status.get(gpio_str)
				if last_logical is not None:
					p.value((1 - last_logical) if reversed_flag else last_logical)
				else:
					p.value(0)
			elif init == 1:
				p.value(0 if reversed_flag else 1)
			elif init == 0:
				p.value(1 if reversed_flag else 0)
			elif was_output and self.status.get(gpio_str) is not None:
				last_logical = self.status.get(gpio_str)
				p.value((1 - last_logical) if reversed_flag else last_logical)
			else:
				p.value(0)
		else:  # input
			pullup = conf.get("pullup", "none" if gpio in INPUT_ONLY_PINS else "up")
			pull = machine.Pin.PULL_DOWN if pullup == "down" else (None if pullup == "none" else machine.Pin.PULL_UP)
			p = machine.Pin(gpio, machine.Pin.IN, pull)

			init = conf.get("init")
			if init is not None and conf.get("watched"):
				ref = self.status.get(gpio_str) if init == "last" else init
				if ref is not None:
					logical = (1 - p.value()) if reversed_flag else p.value()
					if logical != ref:
						self._trigger_webhook(gpio_str)

		self.pins[gpio_str] = p
		watched = conf.get("watched")
		if watched and pin_type not in ("vcc", "gnd"):
			if pin_type == "input":
				self._setup_irq(gpio_str, p)
			else:
				self.watches[gpio_str] = True

	# Watches both edges. The interrupt only schedules _on_watch_trigger, which
	# runs outside interrupt context where allocating memory is allowed.
	def _setup_irq(self, gpio_str, pin):
		def handler(p, _g=gpio_str):
			try:
				micropython.schedule(self._on_watch_trigger, _g)
			except Exception:
				pass
		try:
			pin.irq(trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING, handler=handler)
			self.watches[gpio_str] = True
		except Exception:
			pass

	# At most one webhook per pin every WEBHOOK_MIN_MS (debounce).
	def _on_watch_trigger(self, gpio_str):
		now  = utime.ticks_ms()
		last = self._last_watch_ms.get(gpio_str)
		if last is not None and utime.ticks_diff(now, last) < WEBHOOK_MIN_MS:
			return
		self._last_watch_ms[gpio_str] = now
		self._trigger_webhook(gpio_str)

	# Stops watching a pin (removes its interrupt handler).
	def _clear_watch(self, gpio_str):
		pin = self.pins.get(gpio_str)
		if pin:
			try:
				pin.irq(handler=None)
			except Exception:
				pass
		self.watches.pop(gpio_str, None)

	# Accepts a GPIO number ("4"), which must be usable on this chip, or a
	# configured pin's name ("relay"), case-insensitive. None for an unknown name.
	def resolve_gpio(self, name_or_gpio):
		if name_or_gpio.isdigit():
			_validate_gpio(int(name_or_gpio))
			return name_or_gpio
		lo = name_or_gpio.lower()
		for gpio_str, conf in self.pin_configs.items():
			if (conf.get("name") or "").lower() == lo:
				return gpio_str
		return None

	def _is_static(self, gpio_str):
		return self.pin_configs.get(gpio_str, {}).get("type") in ("vcc", "gnd")

	def _is_status_led(self, gpio_str):
		# The blinker owns the LED pin until a client configures it; reading it as
		# an input would switch its driver off and silence the LED until reboot.
		return BLINKER._led is not None and gpio_str == str(LED_PIN) and gpio_str not in self.pin_configs

	# Builds the response for a pin read. `value` is the logical state (what the caller
	# configured/expects); `level` is the raw physical pin state - they differ only when
	# `reversed` (active-low) is set, and `level` is included only when it's informative.
	# An unconfigured pin is read as an input with pull-up.
	def get_pin_info(self, gpio_str, show_level=False):
		conf          = self.pin_configs.get(gpio_str, {})
		pin           = self.pins.get(gpio_str)
		pin_type      = conf.get("type", "input")
		reversed_flag = conf.get("reversed", False) if pin_type not in ("vcc", "gnd") else False

		if pin is None and self._is_status_led(gpio_str):
			pin, pin_type = BLINKER._led, "output"
		elif pin is None:
			try:
				pull = machine.Pin.PULL_UP if conf.get("pullup", "up") != "down" else machine.Pin.PULL_DOWN
				pin  = machine.Pin(int(gpio_str), machine.Pin.IN, pull)
				self.pins[gpio_str] = pin
			except Exception:
				return None

		level = pin.value()
		value = (1 - level) if reversed_flag else level

		info = {"gpio": int(gpio_str), "value": value, "type": pin_type}
		if show_level or reversed_flag:
			info["level"] = level
		if conf.get("name"):
			info["name"]    = conf["name"]
		if conf.get("init") is not None:
			info["init"]    = conf["init"]
		if pin_type == "input" and conf.get("pullup"):
			info["pullup"]  = conf["pullup"]
		if conf.get("max"):
			info["max"]     = conf["max"]
		if pin_type not in ("vcc", "gnd"):
			info["reversed"] = reversed_flag
		if self.watches.get(gpio_str):
			info["watched"] = True
		return info

	# GET /gpio/read: every configured pin.
	def read_all(self):
		return {g: self.get_pin_info(g) for g in self.pin_configs}

	# One pin by number or name, or every configured pin for "all".
	def read_pin(self, name_or_gpio):
		if name_or_gpio.lower() == "all":
			return self.read_all()
		gpio = self.resolve_gpio(name_or_gpio)
		if gpio is None:
			raise PinNotFound(f"Pin not found: {name_or_gpio}")
		info = self.get_pin_info(gpio)
		if info is None:
			raise ValueError(f"Could not read GP{name_or_gpio}")
		return info

	# Writes 0/1, "on"/"off" or "toggle", with `reversed` applied. An unconfigured
	# pin becomes an output; a pin configured as anything else is refused. A duration
	# (capped by the pin's max, or max alone) reverts the pin to its previous level later.
	def write_pin(self, name_or_gpio, value, duration=None):
		gpio = self.resolve_gpio(name_or_gpio)
		if gpio is None:
			raise PinNotFound(f"Pin not found: {name_or_gpio}")
		if self._is_static(gpio):
			raise ValueError(f"GP{gpio} is vcc/gnd and cannot be written")

		conf          = self.pin_configs.get(gpio, {})
		reversed_flag = conf.get("reversed", False)
		pin           = self.pins.get(gpio)

		if pin is None or conf.get("type") != "output":
			if conf.get("type") not in (None, "output"):
				raise ValueError(f"GP{gpio} is configured as {conf.get('type')} and cannot be written")
			self._clear_watch(gpio)
			pin = machine.Pin(int(gpio), machine.Pin.OUT)
			self.pins[gpio] = pin
			self.pin_configs.setdefault(gpio, {})["type"] = "output"
			self._save_config()
			self.log(f"[GPIO] GP{gpio} auto-configured as output")

		prev = pin.value()
		self._cancel_revert(gpio)

		# "toggle" flips the current state; "on"/"off" are word spellings of 1/0. Any
		# other string fails int() below and is answered 400.
		sval = str(value).lower()
		if sval == "toggle":
			pin.value(1 - pin.value())
		else:
			if sval in ("on", "off"):
				logical = 1 if sval == "on" else 0
			else:
				logical = 1 if int(value) != 0 else 0
			physical = (1 - logical) if reversed_flag else logical
			pin.value(physical)

		self.log(f"[GPIO] GP{gpio} written (physical={pin.value()})")
		self._save_status()

		if self.watches.get(gpio):
			self._trigger_webhook(gpio)

		max_dur   = conf.get("max")
		effective = (min(duration, max_dur) if duration is not None else max_dur) if max_dur else duration

		info = self.get_pin_info(gpio)
		if effective:
			info["duration"] = effective
			self._pending_reverts.append(
				(utime.ticks_add(utime.ticks_ms(), int(effective * 1000)), gpio, pin, prev))
		return info

	# A new write or config supersedes a pending revert for the same pin.
	def _cancel_revert(self, gpio):
		self._pending_reverts = [r for r in self._pending_reverts if r[1] != gpio]

	# Called from the main loop: restores pins whose duration has expired, unless the
	# pin was reconfigured meanwhile.
	def process_reverts(self):
		if not self._pending_reverts:
			return
		now  = utime.ticks_ms()
		keep = []
		for item in self._pending_reverts:
			expire, gpio_str, pin, restore = item
			if utime.ticks_diff(now, expire) < 0:
				keep.append(item)
				continue
			if self.pins.get(gpio_str) is pin:
				try:
					pin.value(restore)
					self.log(f"[GPIO] GP{gpio_str} reverted (duration expired)")
					self._save_status()
					if self.watches.get(gpio_str):
						self._trigger_webhook(gpio_str)
				except Exception:
					pass
		self._pending_reverts = keep

	# POST /gpio/config: merges the update into the pin's config, drops fields that
	# don't apply to its type, and re-initializes the pin. type="remove" deletes it.
	def config_pin(self, name_or_gpio, update):
		if "name" in update:
			update["name"] = _as_text(update["name"])
		gpio_str = self.resolve_gpio(name_or_gpio)
		if gpio_str is None:
			raise PinNotFound(f"Pin not found: {name_or_gpio}")

		self._cancel_revert(gpio_str)

		# type="remove" deletes the pin's config, watch and saved level entirely.
		if update.get("type") == "remove":
			self._clear_watch(gpio_str)
			try:
				machine.Pin(int(gpio_str), machine.Pin.IN, None)
			except Exception:
				pass
			self.pins.pop(gpio_str, None)
			self.pin_configs.pop(gpio_str, None)
			self.status.pop(gpio_str, None)
			self._save_config()
			self.log(f"[GPIO] GP{gpio_str} config removed")
			return {"status": "removed"}

		prev_type      = (self.pin_configs.get(gpio_str) or {}).get("type")
		current        = dict(self.pin_configs.get(gpio_str, {}))
		effective_type = update.get("type") or current.get("type")

		for k, v in update.items():
			if k == "max"      and effective_type == "input":           continue
			if k == "reversed" and effective_type in ("vcc", "gnd"):    continue
			if k == "max" and v is not None:
				try: v = float(v)
				except Exception: pass
			if k == "init":
				v = _check_init(_norm_init(v))
			current[k] = v

		# Clear out any leftover fields that don't make sense for the new type.
		if effective_type in ("vcc", "gnd"):
			current.pop("pullup", None)
			current["reversed"] = False
			current.pop("watched", None)
			self._clear_watch(gpio_str)
		elif effective_type == "output":
			current.pop("pullup", None)
			if not current.get("max"):
				current.pop("max", None)
		elif effective_type == "input":
			current.pop("max", None)

		if current.get("watched") is not True:
			current.pop("watched", None)

		# An init from a previous type means something else for the new one, so drop it
		# unless this call sets a fresh one.
		if prev_type and effective_type != prev_type and "init" not in update:
			current.pop("init", None)

		_validate_gpio(int(gpio_str), current)
		self.pin_configs[gpio_str] = current
		self._save_config()
		self._init_pin(gpio_str, current, was_output=(prev_type == "output"))
		self.log(f"[GPIO] GP{gpio_str} configured: {update}")
		return self.get_pin_info(gpio_str)

	# GET /gpio/scan: every usable pin on this chip, configured or not. An unconfigured
	# pin is read as an input with pull-up (none on input-only pins), then released;
	# the status LED is reported as the blinker's output.
	def scan_all(self):
		result = []
		for gpio in GPIO_PINS:
			gpio_str = str(gpio)
			try:
				conf          = self.pin_configs.get(gpio_str, {})
				pin_type      = conf.get("type", "input")
				reversed_flag = conf.get("reversed", False) if pin_type not in ("vcc", "gnd") else False

				pin  = self.pins.get(gpio_str)
				probe = pin is None
				if probe and self._is_status_led(gpio_str):
					pin, pin_type, probe = BLINKER._led, "output", False
				if probe:
					pin = machine.Pin(gpio, machine.Pin.IN, None if gpio in INPUT_ONLY_PINS else machine.Pin.PULL_UP)

				level = pin.value()
				value = (1 - level) if reversed_flag else level

				if probe:
					machine.Pin(gpio, machine.Pin.IN, None)

				entry = {"gpio": gpio, "level": level, "value": value, "type": pin_type}
				if conf.get("name"):
					entry["name"] = conf["name"]
				if pin_type == "input" and conf.get("pullup"):
					entry["pullup"] = conf["pullup"]
				if pin_type not in ("vcc", "gnd"):
					entry["reversed"] = reversed_flag
				result.append(entry)
			except Exception:
				pass
		return {"agent": self._agent_info(), "gpios": result}

	# GET /gpio/watched: the watched pins and the delivery URLs (never their keys).
	def get_watched_pins(self):
		watched = {g: self.get_pin_info(g) for g, w in self.watches.items() if w}
		bridges = self.config.get("bridges", {})
		return {"watched": watched,
		        "notifications": {"webhook": self.config.get("notifications", {}).get("webhook") or ""},
		        "bridges": {"matter":     bridges.get("matter") or "",
		                    "homekit":    bridges.get("homekit") or "",
		                    "homebridge": bridges.get("homebridge") or ""}}

	# The chip's die temperature in °C (raw_temperature() reports °F), or None.
	def _cpu_temperature(self):
		try:
			import esp32
			if hasattr(esp32, "mcu_temperature"):
				return round(esp32.mcu_temperature(), 1)
			fahrenheit = esp32.raw_temperature()
			return round((fahrenheit - 32) * 5 / 9, 1)
		except Exception:
			return None

	# e.g. "esp32 1.29.0 MicroPython".
	def _get_os(self):
		try:
			u = os.uname()
			return f"{u.sysname} {u.release} MicroPython"
		except Exception:
			return "MicroPython"

	# The chip's unique id as hex.
	def _serial(self):
		try:
			return "".join("{:02x}".format(b) for b in machine.unique_id())
		except Exception:
			return ""

	# "esp32-" plus the serial's last 4 hex digits, used when no name is set.
	def _default_name(self):
		serial = self._serial()
		return ("esp32-" + serial[-4:]) if serial else "esp32"

	# Live, non-persisted facts about the machine this agent runs on. Returned under
	# `agent` by /config/read and /gpio/scan, and never stored in config.json - which
	# is why _normalize_config() drops it from anything posted back to /config/*.
	def _agent_info(self):
		wlan = network.WLAN(network.STA_IF)
		connected = wlan.isconnected()
		ip = wlan.ifconfig()[0] if connected else "unknown"
		ssid, signal = "", None
		if connected:
			for field in ("essid", "ssid"):
				try:
					ssid = wlan.config(field) or ""
					if isinstance(ssid, bytes):
						ssid = ssid.decode()
					break
				except Exception:
					continue
			try:
				signal = wlan.status("rssi")
			except Exception:
				signal = None
		try:
			mac = ":".join("{:02x}".format(b) for b in wlan.config("mac"))
		except Exception:
			mac = ""
		return {"platform": PLATFORM,
		        "os": self._get_os(),
		        "host": "esp32",
		        "serial": self._serial(),
		        "ip": ip,
		        "mac": mac,
		        "wifi": ssid,
		        "signal": signal,
		        "cpu_temperature": self._cpu_temperature(),
		        "uptime": _format_uptime(utime.time() - _BOOT_TIME),
		        "version": VERSION,
		        "config_updated": self._config_updated()}

	# config.json's modification time, or "" before NTP has set the clock.
	def _config_updated(self):
		try:
			t = utime.localtime(os.stat(CONFIG_FILE)[8])
			if t[0] < 2023:
				return ""
			return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*t[:6])
		except Exception:
			return ""

	# The GET /config/read response. Never includes api_key.
	def get_config(self):
		notif    = self.config.get("notifications", {})
		bridges  = self.config.get("bridges", {})
		settings = self.config.get("settings", {})
		cfg = {
			"name":          self.config.get("name") or self._default_name(),
			"agent":         self._agent_info(),
			"notifications": {
				"webhook":     notif.get("webhook") or "",
				"webhook_key": notif.get("webhook_key") or "",
			},
			"bridges": {
				"matter":       bridges.get("matter") or "",
				"matter_key":   bridges.get("matter_key") or "",
				"homekit":      bridges.get("homekit") or "",
				"homekit_key":  bridges.get("homekit_key") or "",
				"homebridge":     bridges.get("homebridge") or "",
				"homebridge_key": bridges.get("homebridge_key") or "",
			},
			"settings": {
				"logs_enabled": settings.get("logs_enabled", True),
				"log_days":     settings.get("log_days", DEFAULT_LOG_DAYS),
			},
			"sensors":       self.config.get("sensors", {}),
		}
		cfg["gpios"] = self.pin_configs
		return cfg

	# POST /sensor/config: sets a sensor's script, or removes it with remove: true.
	def config_sensor(self, name, update):
		if update.get("remove"):
			existed = self.config["sensors"].pop(name, None) is not None
			self._save_config()
			self.log(f"[Sensor] {name} removed")
			return {"status": "removed" if existed else "not found"}
		script = _as_text(update.get("script"))
		if not script:
			raise ValueError("Configure 'script', or 'remove': true to delete the sensor")
		err = _sensor_script_parts(script)[0]
		if err:
			raise ValueError(err)
		self.config["sensors"][name] = {"script": script}
		self._save_config()
		self.log(f"[Sensor] {name} configured: {script}")
		return {"name": name, "script": script}

	# Runs one sensor's script and returns its output nested under the sensor's name.
	# The script is a .py run in-process: read(*args) if it defines one, else its
	# `result` variable; a single-field dict contributes just its value. Re-validates
	# on every read: one that fails reads as {name: "blocked: <reason>"}, a missing
	# or silent one as {name: "<reason>"}.
	def _read_sensor_value(self, name, conf):
		err, script_file, args = _sensor_script_parts(conf.get("script"))
		if err:
			return {name: "blocked: " + err}
		try:
			src = open(script_file).read()
		except OSError:
			return {name: "script not found"}
		try:
			ns = {"args": args}
			exec(src, ns)
			if callable(ns.get("read")):
				val = ns["read"](*args)
			elif "result" in ns:
				val = ns["result"]
			else:
				val = None
		except Exception:
			return {name: "script failed or did not produce output"}
		if val is None:
			return {name: "script failed or did not produce output"}
		if isinstance(val, dict) and len(val) == 1:
			val = list(val.values())[0]
		return {name: val}

	# GET /sensor/read/{name}.
	def read_sensor(self, name):
		conf = self.config["sensors"].get(name)
		if conf is None:
			raise SensorNotFound(f"Sensor not found: {name}")
		return self._read_sensor_value(name, conf)

	# GET /sensor/read: every sensor, one key each.
	def read_sensors(self):
		result = {}
		for name, conf in self.config["sensors"].items():
			try:
				result.update(self._read_sensor_value(name, conf))
			except Exception as e:
				result[name] = f"error: {e}"
		return result

	# Applies a partial config update (POST /config/update). The body may spell each
	# grouped field either inside its group or at the root (_normalize_config folds the
	# root form in). Every group merges key by key rather than replacing, so a bridge
	# pushing its own URL never clears the other bridge's, and setting log_days never
	# resets logs_enabled. api_key only changes on a real value; log_days is clamped to
	# 0-30 and 0 wipes the log immediately; an empty string clears a URL/key field.
	def update_config(self, updates):
		updates = _normalize_config(updates)
		if not HTTPS_WEBHOOK_OK and _is_https((updates.get("notifications") or {}).get("webhook")):
			raise ValueError(HTTPS_WEBHOOK_ERROR)
		self._backup_config()
		for k, v in updates.items():
			if k == "api_key":
				if v:
					self.config["api_key"] = v
			elif k in ("notifications", "bridges"):
				allowed = (("webhook", "webhook_key") if k == "notifications"
				           else ("matter", "matter_key", "homekit", "homekit_key",
				                 "homebridge", "homebridge_key"))
				group = self.config.setdefault(k, {})
				for gk, gv in (v or {}).items():
					if gk in allowed and gv is not None:
						group[gk] = None if gv == "" else gv
			elif k == "settings":
				settings = self.config.setdefault("settings", {})
				for sk, sv in (v or {}).items():
					if sv is None:
						continue
					if sk == "log_days":
						days = max(0, min(int(sv), 30))
						settings["log_days"] = days
						if days == 0:
							self.clear_logs()
					elif sk == "logs_enabled":
						settings[sk] = sv
			elif k == "name":
				self.config[k] = v
		self._save_config()
		return self.get_config()

	# Replaces the whole config file (POST /config/load); an empty dict resets
	# everything to defaults while preserving the current API key. Written at once.
	def load_config(self, new_config):
		self._backup_config()
		new_config = _normalize_config(new_config)
		notif = new_config.get("notifications")
		if not HTTPS_WEBHOOK_OK and isinstance(notif, dict) and _is_https(notif.get("webhook")):
			self.log(f"[Config] Dropped https webhook URL (not supported on this board): {notif.get('webhook')}")
			notif["webhook"] = None
		new_config["api_key"] = new_config.get("api_key", self.config.get("api_key", DEFAULT_API_KEY))
		new_config.setdefault("gpios", {})
		new_config.setdefault("sensors", {})
		new_config.setdefault("settings", {})
		new_config["settings"].setdefault("log_days", DEFAULT_LOG_DAYS)
		with open(CONFIG_FILE, "w") as f:
			ujson.dump(new_config, f)
		self._reload()
		return self.get_config()

	# Stops every watch and re-initializes all pins from the config on flash.
	def _reload(self):
		for gpio_str in list(self.watches):
			self._clear_watch(gpio_str)
		self.pins.clear()
		self._load_config()
		self._load_status()
		self._initialize_pins()

	# Adds a timestamped line to the in-memory log, unless logging is off.
	def log(self, msg):
		if not self.config.get("settings", {}).get("logs_enabled", True):
			return
		try:
			t     = utime.localtime()
			entry = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} {}".format(*t[:6], msg)
		except Exception:
			entry = msg
		self._log.append(entry)
		if len(self._log) > LOG_MAX:
			self._log.pop(0)

	# GET /logs: the newest LOG_RETURN lines.
	def get_logs(self):
		return self._log[-LOG_RETURN:]

	def clear_logs(self):
		self._log.clear()

	# Hourly from the main loop: drops lines older than log_days (an hour for 0).
	def maybe_cleanup_logs(self):
		now = utime.time()
		if now - self._last_log_cleanup < 3600:
			return
		self._last_log_cleanup = now
		days   = self.config.get("settings", {}).get("log_days", DEFAULT_LOG_DAYS)
		offset = 3600 if days == 0 else days * 86400
		cutoff = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*utime.localtime(now - offset)[:6])
		self._log = [e for e in self._log if len(e) <= 19 or e[:19] >= cutoff]

	# Every place a watched event should be delivered, as (url, api_key) pairs:
	# the general-purpose webhook_url plus each smart-home bridge's own callback
	# field. A bridge sets only its own field, so bridges never overwrite each other.
	def _notify_targets(self):
		targets = []
		notif = self.config.get("notifications", {})
		if notif.get("webhook"):
			targets.append((notif.get("webhook"), notif.get("webhook_key")))
		bridges = self.config.get("bridges", {})
		for field in ("matter", "homekit", "homebridge"):
			url = bridges.get(field)
			if url:
				targets.append((url, bridges.get(field + "_key") or None))
		return targets

	# Queues the pin's state for every target; process_webhooks() sends it. With no
	# target configured the event is only logged.
	def _trigger_webhook(self, gpio_str):
		targets = self._notify_targets()
		if targets:
			for url, api_key in targets:
				self._webhook_queue.append((gpio_str, 0, url, api_key, utime.ticks_ms()))
		else:
			self.log("[Webhook] Not sent (no notifications.webhook configured), GPIO: " + gpio_str)

	# The "Up and running" notification, sent once at boot.
	def queue_boot_webhook(self):
		for url, api_key in self._notify_targets():
			self._webhook_queue.append((None, 0, url, api_key, utime.ticks_ms()))

	# Called from the main loop: sends the first queued webhook that is due. A failed
	# one is queued again WEBHOOK_RETRY_MS later, up to WEBHOOK_RETRIES times.
	def process_webhooks(self):
		if not self._webhook_queue:
			return
		now = utime.ticks_ms()
		for i, entry in enumerate(self._webhook_queue):
			if utime.ticks_diff(now, entry[4]) >= 0:
				gpio_str, attempts, url, api_key, _ = self._webhook_queue.pop(i)
				break
		else:
			return
		if not url:
			return
		try:
			payload = {"agent": self.config.get("name") or self._default_name()}
			if gpio_str is None:
				payload["note"] = "Up and running"
			else:
				info = self.get_pin_info(gpio_str)
				if info:
					payload.update(info)
			if attempts == 0:
				self.log(f"[Webhook] {'boot' if gpio_str is None else 'GPIO ' + gpio_str} → {url}")
			self._webhook_sender.send(url, payload, api_key)
		except Exception as e:
			self.log(f"[WEBHOOK] GP{gpio_str} failed: {type(e).__name__} {repr(e)}")
			if attempts < WEBHOOK_RETRIES:
				self._webhook_queue.append((gpio_str, attempts + 1, url, api_key,
				                            utime.ticks_add(utime.ticks_ms(), WEBHOOK_RETRY_MS)))

# Wi-Fi

# wifi.txt holds SSID=... and PASSWORD=... lines; # starts a comment.
def _read_wifi(path="wifi.txt"):
	settings = {}
	try:
		with open(path) as f:
			for line in f:
				line = line.strip()
				if not line or line.startswith("#") or "=" not in line:
					continue
				key, _, val = line.partition("=")
				settings[key.strip()] = val.strip().strip('"').strip("'")
	except OSError:
		pass
	return settings

# Joins the Wi-Fi from wifi.txt, waiting up to 20 s, and resets the board if that
# fails. Returns the IP address.
def connect_wifi():
	settings = _read_wifi()
	ssid     = settings.get("SSID")     or FALLBACK_SSID
	password = settings.get("PASSWORD") or FALLBACK_PASSWORD

	if not ssid:
		print("[Wi-Fi] ERROR: No SSID configured in wifi.txt")
		return "0.0.0.0"

	wlan = network.WLAN(network.STA_IF)
	if wlan.active() and not wlan.isconnected():
		try:
			wlan.disconnect()
		except OSError:
			pass
		wlan.active(False)
		utime.sleep(0.1)
	wlan.active(True)
	if not wlan.isconnected():
		print(f"[Wi-Fi] Connecting to '{ssid}'...", end="")
		try:
			wlan.connect(ssid, password)
		except OSError:
			utime.sleep(0.5)
			try:
				if not wlan.isconnected():
					wlan.connect(ssid, password)
			except OSError:
				pass
		for _ in range(40):
			if wlan.isconnected():
				break
			print(".", end="")
			utime.sleep(0.5)
		print()

	if not wlan.isconnected():
		status_code = wlan.status()
		status_names = {
			200: "BEACON_TIMEOUT",
			201: "NO_AP_FOUND (check SSID / 2.4GHz)",
			202: "WRONG_PASSWORD",
			203: "ASSOC_FAIL / CONNECT_FAIL",
			204: "HANDSHAKE_TIMEOUT",
			1000: "IDLE",
			1001: "CONNECTING",
		}
		status_desc = status_names.get(status_code, f"status {status_code}")
		print(f"[Wi-Fi] Connection failed ({status_desc}). Resetting board...")
		machine.reset()

	ip = wlan.ifconfig()[0]
	print(f"[Wi-Fi] Connected! IP: {ip}")
	return ip

# Sets the clock over NTP, for log timestamps and config_updated.
def sync_time():
	try:
		import ntptime
		ntptime.settime()
	except Exception:
		pass

# Set again in main() once the clock is synced.
_BOOT_TIME = utime.time()

def _format_uptime(seconds):
	"""Seconds -> the short human string the lab and the dashboards display as-is.
	At most two units, largest first: "3d 4h", "4h 12m", "12m", "38s"."""
	s = int(seconds)
	d, rem = divmod(s, 86400)
	h, rem = divmod(rem, 3600)
	m, sec = divmod(rem, 60)
	if d:
		return f"{d}d {h}h"
	if h:
		return f"{h}h {m}m"
	if m:
		return f"{m}m"
	return f"{sec}s"

# A TCP connect to the gateway's port 80: an answer or a quick refusal means the
# link carries traffic; only a timeout counts as dead.
def _gateway_alive(wlan):
	try:
		gw = wlan.ifconfig()[2]
		s  = usocket.socket()
		s.settimeout(2)
		t0 = utime.ticks_ms()
		try:
			s.connect((gw, 80))
			return True
		except OSError:
			return utime.ticks_diff(utime.ticks_ms(), t0) < 1500
		finally:
			s.close()
	except Exception:
		return True

def _wifi_reconnect(wlan):
	# Non-blocking: connect() returns at once, WifiWatchdog checks the outcome.
	settings = _read_wifi()
	try:
		wlan.disconnect()
	except OSError:
		pass
	try:
		wlan.connect(settings.get("SSID") or FALLBACK_SSID, settings.get("PASSWORD") or FALLBACK_PASSWORD)
	except OSError:
		pass

class WifiWatchdog:
	"""Recovers a link that reports connected but passes nothing. Only while idle: probe the gateway every
	WIFI_PROBE_SECS, reconnect softly after two failed probes (GPIO untouched), and
	reset only if the link is still dead WIFI_RECONNECT_SECS later. A link that
	reports disconnected is the dead-man switch's job, not this one's."""

	def __init__(self, wlan, manager, probe=_gateway_alive, reconnect=_wifi_reconnect,
	             reset=None, now=utime.time):
		self._wlan, self._manager, self._now = wlan, manager, now
		self._probe, self._reconnect = probe, reconnect
		self._reset = reset or self._hard_reset
		self._last_ok = self._last_probe = now()
		self._failures = 0
		self._reconnect_at = None

	def _log(self, msg):
		self._manager.log("[WIFI] " + msg)
		print("[WIFI] " + msg)

	def _hard_reset(self):
		self._manager._flush_all()
		machine.reset()

	def traffic(self):
		# A request got through, so the link works.
		if self._reconnect_at is not None:
			self._log("link recovered")
		self._last_ok = self._now()
		self._failures = 0
		self._reconnect_at = None

	def tick(self, busy=False):
		now = self._now()
		if self._reconnect_at is not None:
			if not busy and now - self._last_probe >= 5 and self._wlan.isconnected():
				self._last_probe = now
				if self._probe(self._wlan):
					self.traffic()
					return
			if now - self._reconnect_at >= WIFI_RECONNECT_SECS:
				self._log("still unreachable after reconnect, resetting")
				self._reset()
			return
		# busy: a pulse revert is pending, and a probe can block for up to 2 s.
		if busy or not self._wlan.isconnected():
			return
		if now - self._last_ok < WIFI_PROBE_SECS or now - self._last_probe < WIFI_PROBE_SECS:
			return
		self._last_probe = now
		if self._probe(self._wlan):
			self._last_ok = now
			self._failures = 0
			return
		self._failures += 1
		self._log("gateway unreachable (%d)" % self._failures)
		if self._failures >= 2:
			self._log("reconnecting")
			self._reconnect_at = now
			self._reconnect(self._wlan)

# The largest free block of system (ESP-IDF) RAM, or None where that cannot be read.
def _largest_free_block():
	try:
		import esp32
		return max(r[2] for r in esp32.idf_heap_info(esp32.HEAP_DATA))
	except Exception:
		return None

class HeapWatch:
	"""Heap dead-man. lwIP and the Wi-Fi driver allocate from system RAM, and a classic ESP32
	that has run short of it stays half alive: it accepts connections but cannot send a
	reply, and nothing else resets it. Every HEAP_CHECK_SECS this reads the largest free
	block; below HEAP_LOW_BYTES for HEAP_LOW_SECS, with no pulse pending, it flushes and
	resets so the board comes back clean."""

	def __init__(self, manager, largest=_largest_free_block, reset=None, now=utime.time):
		self._manager, self._largest, self._now = manager, largest, now
		self._reset = reset or self._hard_reset
		self._checked = now()
		self._low_since = None

	def _log(self, msg):
		self._manager.log("[HEAP] " + msg)
		print("[HEAP] " + msg)

	def _hard_reset(self):
		self._manager._flush_all()
		machine.reset()

	def tick(self, busy=False):
		now = self._now()
		if now - self._checked < HEAP_CHECK_SECS:
			return
		self._checked = now
		largest = self._largest()
		if largest is None or largest >= HEAP_LOW_BYTES:
			if largest is not None and self._low_since is not None:
				self._log("recovered (largest free block %d bytes)" % largest)
			self._low_since = None
			return
		if self._low_since is None:
			self._low_since = now
			self._log("largest free block %d bytes, under %d" % (largest, HEAP_LOW_BYTES))
		elif now - self._low_since >= HEAP_LOW_SECS and not busy:
			self._log("still low after %ds, resetting" % (now - self._low_since))
			self._reset()

# HTTP

# Reads one request within 6 s. Returns (headers, body), or None when it is too
# big, chunked, cut off or late, which the caller answers 400.
def _recv_request(conn):
	data = b""
	body = None  # sized from Content-Length once the headers are in
	got = 0
	conn.settimeout(2)
	deadline = utime.ticks_add(utime.ticks_ms(), 6000)
	while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
		try:
			chunk = conn.recv(1024)
		except OSError as e:
			if e.args and e.args[0] in (uerrno.ETIMEDOUT, uerrno.EAGAIN):
				continue
			break
		except Exception:
			break
		if not chunk:
			break
		if body is None:
			data += chunk
			if len(data) > MAX_REQUEST_BYTES:
				return None
			sep = data.find(b"\r\n\r\n")
			if sep == -1:
				continue
			cl = 0
			chunked = False
			for line in data[:sep].decode("utf-8", "replace").split("\r\n")[1:]:
				low = line.lower()
				if low.startswith("content-length:"):
					try: cl = int(line.split(":", 1)[1].strip())
					except Exception: pass
				elif low.startswith("transfer-encoding:") and "chunked" in low:
					chunked = True
			# A chunked body has no Content-Length, so cl stays 0 and the request would be
			# returned with its body unread - an empty body that /config/load applies as {}.
			# No real client sends one here; refuse it (400) instead of decoding it.
			# Refused requests are not read to the end: the 400 goes out and the socket is
			# closed with the rest unread. Draining up to 64 KB just to discard it filled
			# lwIP's buffers and took a classic ESP32 off the network (api-tester test 54).
			# The Pi answers 413 after reading; a client may see a reset instead here.
			if chunked or cl > MAX_REQUEST_BYTES:
				return None
			if cl == 0:
				# No Content-Length: whatever followed the headers is the body.
				return data[:sep + 4], data[sep + 4:]
			# One allocation of the exact size, so a fragmented heap can still provide it.
			body = bytearray(cl)
			chunk = data[sep + 4:]
			data = data[:sep + 4]
		take = min(len(chunk), len(body) - got)
		body[got:got + take] = chunk[:take]
		got += take
		if got >= len(body):
			return data, body
	return None

# Splits (headers, body) into (method, path, headers, body). body is {} when empty
# and None when it isn't a JSON object, which the caller answers 422. Returns None
# for a malformed request.
def _parse_request(req):
	if not req:
		return None
	head, tail = req
	try:
		lines = head[:-4].decode("utf-8", "replace").split("\r\n")
		parts = lines[0].split(" ", 2)
		if len(parts) < 2:
			return None
		method = parts[0].upper()
		path   = parts[1].split("?")[0]
		hdrs   = {}
		for line in lines[1:]:
			if ":" in line:
				k, v = line.split(":", 1)
				hdrs[k.strip().lower()] = v.strip()
		body = {}
		if len(tail):
			if len(tail) > 1024:
				gc.collect()
			try:
				parsed = ujson.loads(tail)
				body = parsed if isinstance(parsed, dict) else None
			except Exception:
				body = None
		return method, path, hdrs, body
	except Exception:
		return None

def _send_all(conn, data, timeout_ms=3000):
	# A blocking sendall waits forever when lwIP is out of buffers, which wedges the
	# whole agent; a non-blocking loop with a deadline drops the reply instead.
	conn.setblocking(False)
	mv = memoryview(data)
	deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
	while len(mv):
		try:
			n = conn.send(mv)
		except OSError as e:
			if not e.args or e.args[0] != uerrno.EAGAIN:
				raise
			n = 0
		if n:
			mv = mv[n:]
		elif utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
			raise OSError(uerrno.ETIMEDOUT)
		else:
			utime.sleep_ms(10)

# Sends a status line with no body: the empty 404 every unknown route or pin gets.
def _send_empty(conn, status=404):
	header = (
		f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\n"
		f"Content-Length: 0\r\n"
		f"Connection: close\r\n\r\n"
	).encode()
	try:
		_send_all(conn, header)
	except Exception:
		pass

# Yields the JSON text of obj in small pieces that join to exactly ujson.dumps(obj).
# The top two levels (the reply object and its lists/maps, e.g. scan's gpios) are split;
# anything deeper is one piece.
def _json_pieces(obj, depth=2):
	if depth and isinstance(obj, dict):
		yield "{"
		first = True
		for k, v in obj.items():
			yield ("" if first else ", ") + ujson.dumps(k) + ": "
			first = False
			yield from _json_pieces(v, depth - 1)
		yield "}"
	elif depth and isinstance(obj, list):
		yield "["
		for i, v in enumerate(obj):
			if i:
				yield ", "
			yield from _json_pieces(v, depth - 1)
		yield "]"
	else:
		yield ujson.dumps(obj)

# Sends a JSON response. Every response closes the connection (no keep-alive).
# The body is never built as one string: it is sized from its pieces first, then sent
# in writes of about SEND_CHUNK bytes. A /gpio/scan reply (~2 KB) built whole, plus its
# bytes copy, was the allocation that a classic ESP32 short of system RAM could not
# send (api-tester test 17).
SEND_CHUNK = 512

def _send_json(conn, data, status=200):
	size = 0
	for piece in _json_pieces(data):
		size += len(piece.encode())
	out = [(
		f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\n"
		f"Content-Type: application/json\r\n"
		f"Content-Length: {size}\r\n"
		f"Connection: close\r\n\r\n"
	).encode()]
	n = len(out[0])
	try:
		for piece in _json_pieces(data):
			b = piece.encode()
			out.append(b)
			n += len(b)
			if n >= SEND_CHUNK:
				_send_all(conn, b"".join(out))
				out, n = [], 0
		if out:
			_send_all(conn, b"".join(out))
	except Exception:
		pass

# Upgrade

# Removes a file if it exists.
def _rm(path):
	try:
		os.remove(path)
	except Exception:
		pass

# None if the file looks right (a .mpy header, or Python text) and has the expected
# size, else the reason.
def _verify_download(path, size, kind):
	try:
		with open(path, "rb") as f:
			head = f.read(256)
			if kind == "mpy":
				if not head.startswith(b"M"):
					return "invalid mpy header"
			elif b"import" not in head:
				return "import missing"
			f.seek(0)
			total = 0
			while True:
				chunk = f.read(512)
				if not chunk:
					break
				if kind == "py" and b"\x00" in chunk:
					return "binary data in file"
				total += len(chunk)
	except Exception as e:
		return "unreadable: " + str(e)
	if total != size:
		return f"size mismatch (wrote {size}, read back {total})"
	return None

# Downloads url to dest (http or https, certificate not verified) with a
# Content-Length, chunked or read-to-close body. Returns the byte count; raises on
# any error or short read.
def _http_download(url, dest):
	proto, rest = url.split("://", 1)
	host_port, _, path = rest.partition("/")
	host, _, port = host_port.partition(":")
	port = int(port) if port else (443 if proto == "https" else 80)
	if proto == "https" and not _HAS_SSL:
		raise OSError("no ssl module")
	gc.collect()
	s = usocket.socket()
	s.settimeout(15)
	try:
		s.connect(usocket.getaddrinfo(host, port)[0][-1])
		if proto == "https":
			ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
			ctx.verify_mode = ssl.CERT_NONE
			s = ctx.wrap_socket(s, server_hostname=host)
		s.write(("GET /{} HTTP/1.1\r\nHost: {}\r\nUser-Agent: ctrlpi-esp32\r\n"
		         "Accept: */*\r\nConnection: close\r\n\r\n").format(path, host).encode())
		line = s.readline()
		if not line or not line.startswith(b"HTTP/"):
			raise OSError("bad response")
		status = int(line.split(b" ")[1])
		if status != 200:
			raise OSError(f"HTTP {status}")
		cl, chunked = None, False
		while True:
			line = s.readline()
			if not line or line == b"\r\n":
				break
			ll = line.lower()
			if ll.startswith(b"content-length:"):
				cl = int(line.split(b":", 1)[1])
			elif ll.startswith(b"transfer-encoding:") and b"chunked" in ll:
				chunked = True
		total = 0
		with open(dest, "wb") as f:
			if chunked:
				complete = False
				while True:
					size_line = s.readline()
					if not size_line:
						break
					size = int(size_line.strip().split(b";")[0], 16)
					if size == 0:
						complete = True
						break
					left = size
					while left > 0:
						chunk = s.read(min(left, 512))
						if not chunk:
							raise OSError("short read")
						f.write(chunk); total += len(chunk); left -= len(chunk)
					s.readline()
				if not complete:
					raise OSError("truncated chunked body")
			elif cl is not None:
				left = cl
				while left > 0:
					chunk = s.read(min(left, 512))
					if not chunk:
						break
					f.write(chunk); total += len(chunk); left -= len(chunk)
				if left > 0:
					raise OSError("short read")
			else:
				while True:
					chunk = s.read(512)
					if not chunk:
						break
					f.write(chunk); total += len(chunk)
		return total
	finally:
		try:
			s.close()
		except Exception:
			pass

# POST /upgrade: downloads every file in UPGRADE_FILES as <name>.new and checks
# each one. Only when all are good are they swapped in, keeping <name>.backup; if
# any step of the swap fails, the backups are put back. The caller resets the
# board on success.
def _do_upgrade(manager):
	downloaded = []
	for filename, kind, minimum in UPGRADE_FILES:
		tmp = filename + ".new"
		url = UPGRADE_BASE_URL + filename
		manager.log("[UPGRADE] downloading " + filename)
		try:
			n = _http_download(url, tmp)
		except Exception as e:
			for item in UPGRADE_FILES:
				_rm(item[0] + ".new")
			return (False, {"error": filename + " download failed: " + str(e)}, 502)
		if n < minimum:
			for item in UPGRADE_FILES:
				_rm(item[0] + ".new")
			return (False, {"error": filename + f" is too small ({n} bytes)"}, 502)
		bad = _verify_download(tmp, n, kind)
		if bad:
			for item in UPGRADE_FILES:
				_rm(item[0] + ".new")
			return (False, {"error": filename + " failed verification: " + bad}, 502)
		downloaded.append((filename, kind, n))

	installed = []
	try:
		for filename, kind, n in downloaded:
			bak = filename + ".backup"
			_rm(bak)
			os.rename(filename, bak)
			installed.append((filename, kind, n))
			os.rename(filename + ".new", filename)
		for filename, kind, n in downloaded:
			bad = _verify_download(filename, n, kind)
			if bad:
				raise OSError(filename + ": " + bad)
	except Exception as e:
		recovery = "restored previous files"
		try:
			for filename, kind, n in installed:
				bak = filename + ".backup"
				try:
					os.stat(bak)
				except OSError:
					continue
				_rm(filename)
				os.rename(bak, filename)
		except Exception as restore_error:
			recovery = "restore failed: " + str(restore_error)
		for item in UPGRADE_FILES:
			_rm(item[0] + ".new")
		manager.log("[UPGRADE] install failed; " + recovery)
		return (False, {"error": "install failed: " + str(e), "recovery": recovery}, 500)

	manager.log("[UPGRADE] app.mpy, boot.py and main.py replaced")
	return (True, {"status": "upgrading"}, 200)

# Requests

# Serves one request and always closes the socket. Running out of memory answers
# 500 instead of leaving the client waiting.
def handle_request(conn, manager):
	BLINKER.blink(2)
	try:
		_handle_request(conn, manager)
	except MemoryError:
		gc.collect()
		manager.log("[ERROR] Out of memory while handling a request")
		_send_json(conn, {"error": "Out of memory"}, 500)
	finally:
		try:
			conn.close()
		except Exception:
			pass

# Reads and parses the request, checks the Api-Key (every route but /hello), and
# dispatches on method and path. Same routes, status codes and JSON as
# pi-gpio-api; an unknown route or pin answers an empty 404.
def _handle_request(conn, manager):
	parsed = _parse_request(_recv_request(conn))

	if not parsed:
		_send_json(conn, {"error": "Bad request"}, 400)
		conn.close()
		return

	method, path, hdrs, body = parsed
	if manager.config.get("settings", {}).get("logs_enabled", True) and not (method == "GET" and path == "/hello"):
		manager.log(f"[API] Received {method} {path}")
	if body is None:
		_send_json(conn, {"error": "Request body failed validation"}, 422)
		conn.close()
		return
	try:
		if method == "GET" and path == "/hello":
			_send_json(conn, {"name": manager.config.get("name") or manager._default_name()})
			conn.close()
			return

		if hdrs.get("api-key") != manager.config.get("api_key", DEFAULT_API_KEY):
			_send_json(conn, {"error": "Invalid or missing Api-Key header"}, 403)
			conn.close()
			return

		if   method == "GET"  and path == "/gpio/read":
			_send_json(conn, manager.read_all())
		elif method == "GET"  and path.startswith("/gpio/read/"):
			_send_json(conn, manager.read_pin(path[len("/gpio/read/"):]))
		elif method == "POST" and path.startswith("/gpio/write/"):
			if body.get("value") is None:
				_send_json(conn, {"error": "Request body failed validation"}, 422)
				conn.close()
				return
			_send_json(conn, manager.write_pin(path[len("/gpio/write/"):], body.get("value"), body.get("duration")))
		elif method == "POST" and path.startswith("/gpio/config/"):
			_send_json(conn, manager.config_pin(path[len("/gpio/config/"):], body))
		elif method == "GET"  and path == "/gpio/watched":
			_send_json(conn, manager.get_watched_pins())
		elif method == "POST" and path.startswith("/sensor/config/"):
			_send_json(conn, manager.config_sensor(path[len("/sensor/config/"):], body))
		elif method == "GET"  and path == "/sensor/read":
			_send_json(conn, manager.read_sensors())
		elif method == "GET"  and path.startswith("/sensor/read/"):
			_send_json(conn, manager.read_sensor(path[len("/sensor/read/"):]))
		elif method == "GET"  and path == "/gpio/scan":
			_send_json(conn, manager.scan_all())
		elif method == "GET"  and path == "/config/read":
			_send_json(conn, manager.get_config())
		elif method == "POST" and path == "/config/update":
			_send_json(conn, manager.update_config(body))
		elif method == "POST" and path == "/config/load":
			_send_json(conn, manager.load_config(body))
		# On success the board resets into the new files.
		elif method == "POST" and path == "/upgrade":
			ok, payload, status = _do_upgrade(manager)
			_send_json(conn, payload, status)
			if ok:
				conn.close()
				manager._flush_all()
				utime.sleep_ms(500)
				machine.reset()
		# reboot: true resets the board; otherwise pins and config reload in place.
		elif method == "POST" and path == "/restart":
			reboot = bool(body.get("reboot", False))
			_send_json(conn, {"status": "rebooting" if reboot else "restarting"})
			conn.close()
			manager._flush_all()
			if reboot:
				utime.sleep_ms(500)
				try:
					machine.reset()
				except Exception as e:
					manager.log(f"[CONFIG] Reboot failed: {e}; falling back to soft restart")
					try:
						machine.soft_reset()
					except Exception:
						manager._reload()
						manager.log("[BOOT] In-process restart via /restart")
			else:
				manager._reload()
				manager.log("[BOOT] In-process restart via /restart")
		elif method == "GET"  and path == "/logs":
			_send_json(conn, {"logs": manager.get_logs()})
		else:
			_send_empty(conn, 404)

	except PinNotFound:
		_send_empty(conn, 404)
	except Exception as e:
		try:
			_send_json(conn, {"error": str(e)}, 400)
		except Exception:
			pass

	conn.close()

# Boot

# Boot (fast LED blink, Wi-Fi, clock, pins, boot webhook), then the single-threaded
# loop: the deferred work (log trimming, flash writes, pulse reverts, webhooks) and
# one request per accept(). accept() waits 1 s, or 50 ms while a revert is pending.
def main():
	BLINKER.start_fast()
	ip      = connect_wifi()
	sync_time()
	global _BOOT_TIME
	_BOOT_TIME = utime.time()
	manager = GPIOManager()
	manager.queue_boot_webhook()

	addr   = usocket.getaddrinfo("0.0.0.0", PORT)[0][-1]
	server = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
	server.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
	# The port can stay busy briefly after a soft reset: retry, then reset.
	for attempt in range(3):
		try:
			server.bind(addr)
			break
		except OSError as e:
			if e.args[0] in (98, 112):
				if attempt == 2:
					manager.log("[BOOT] Port 8314 in use, hard resetting...")
					utime.sleep(0.5)
					machine.reset()
				manager.log(f"[BOOT] Port in use, retrying ({attempt+1}/2)...")
				utime.sleep(2)
			else:
				raise
	server.listen(3)
	server.settimeout(1)

	manager.log(f"[BOOT] Listening on {ip}:{PORT}")
	print(f"[BOOT] HTTP server listening on http://{ip}:{PORT}")

	wlan         = network.WLAN(network.STA_IF)
	wifi_last_ok = utime.time()
	watchdog     = WifiWatchdog(wlan, manager)
	heap_watch   = HeapWatch(manager)
	boot_blink   = True

	while True:
		try:
			manager.maybe_cleanup_logs()
			manager.maybe_flush()
			manager.process_reverts()
			manager.process_webhooks()
			manager._webhook_sender.maybe_expire()
			# Fast blinking ends once the boot webhook has gone out (or there was none).
			if boot_blink and not any(e[0] is None for e in manager._webhook_queue):
				boot_blink = False
				BLINKER.stop_fast()
			server.settimeout(0.05 if manager._pending_reverts else 1)
			now = utime.time()
			# Dead-man switch: reset when Wi-Fi has been down for WIFI_DEAD_SECS.
			if wlan.isconnected():
				wifi_last_ok = now
			elif now - wifi_last_ok > WIFI_DEAD_SECS:
				manager._flush_all()
				machine.reset()
			watchdog.tick(busy=bool(manager._pending_reverts))
			heap_watch.tick(busy=bool(manager._pending_reverts))
			conn, _ = server.accept()
			watchdog.traffic()
			handle_request(conn, manager)
		except OSError:
			pass
		except Exception as e:
			manager.log(f"[ERROR] Server: {type(e).__name__} {repr(e)}")

main()
