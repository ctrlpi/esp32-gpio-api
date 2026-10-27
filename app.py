# ESP32 GPIO REST API

import gc
import machine
import micropython
from micropython import const
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
_FALLBACK_SSID    = const("MyNetwork")
_FALLBACK_PASSWORD = const("MyPassword")
_DEFAULT_API_KEY  = const("your-secret-key")
VERSION           = "0.9.30"  # API version
_DEFAULT_LOG_DAYS = const(7)
_PORT             = const(8314)
_WIFI_DEAD_SECS   = const(40)      # dead-man switch: reset if WiFi stays lost this long
_NTP_RETRY_MS     = const(120000)  # while NTP hasn't set the clock, try again this often
_WIFI_PROBE_SECS  = const(120)     # idle this long: probe the gateway, then this often
_WIFI_RECONNECT_SECS = const(20)   # a soft reconnect gets this long before the board resets
_HEAP_LOW_BYTES   = const(6144)    # heap dead-man: largest free system-RAM block below this...
_HEAP_LOW_SECS    = const(30)      # ...for this long (no pulse pending) flushes and resets
_HEAP_CHECK_SECS  = const(5)       # how often the main loop looks
_HEAP_BOOT_GRACE_SECS = const(180) # ...and never in the first 3 minutes after boot: a C3 idles near 6 KB then
_WEBHOOK_MIN_MS   = const(200)     # watch debounce
_WEBHOOK_IDLE_MS  = const(90000)   # socket idle timeout
_WEBHOOK_RETRIES  = const(2)       # extra attempts
_WEBHOOK_RETRY_MS = const(30000)   # retry delay
# Files on flash: the config, its last good copy (read when config.json fails to
# parse) and each pin's last logical level (restored at boot for init "last").
_CONFIG_FILE      = const("config.json")
_BACKUP_FILE      = const("config-backup.json")
_STATUS_FILE      = const("status.json")
# POST /upgrade leaves _UPGRADE_LOCK for upgrader.mpy, which downloads UPGRADE_FILES from
# here: (name, kind, minimum size in bytes). It reports to _UPGRADE_RESULT.
UPGRADE_BASE_URL  = "https://raw.githubusercontent.com/ctrlpi/esp32-gpio-api/main/"
UPGRADE_FILES     = (("app.mpy", "mpy", 2000),
                     ("boot.py", "py", 200),
                     ("upgrader.mpy", "mpy", 1000),
                     ("main.py", "py", 20))
_UPGRADE_LOCK     = const("upgrade.lock")
_UPGRADE_RESULT   = const("upgrade-result.json")
# Collect garbage after this many bytes are allocated, not only when an allocation fails: on
# an ESP32 the heap grows into the system RAM the network stack needs when it runs out.
_GC_THRESHOLD     = const(16384)
# Why the agent last reset itself, written just before; logged and removed at the next boot.
_RESET_NOTE       = const("last-reset.txt")
# The log is an in-memory ring of _LOG_MAX lines; /logs returns the newest _LOG_RETURN.
_LOG_MAX          = const(50)
_LOG_RETURN       = const(30)
# Largest request accepted, headers and body together; a bigger one is refused (400).
_MAX_REQUEST_BYTES = const(8192)

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
_LED_TIMER_ID           = const(0)

# An https webhook needs the ssl module, and a TLS handshake needs roughly 35 KB of
# contiguous system RAM. A classic ESP32 has about 24 KB free after boot (largest block
# ~19 KB), so every https POST fails with ENOMEM. An ESP32-C3 with the agent loaded failed
# the same way (MPI_ALLOC_FAILED, X509_ALLOC_FAILED, even right after boot), and its failed
# handshakes hung the board. Like the original Pico W, both refuse https URLs instead of
# storing one that never delivers; only the S3 has the room.
_IS_S3              = "s3" in _CHIP
HTTPS_WEBHOOK_OK    = _HAS_SSL and _IS_S3
HTTPS_WEBHOOK_ERROR = ("https webhooks need the ssl module" if _IS_S3 else
                       "https webhooks are not supported on an ESP32-C3: use http://" if "c3" in _CHIP else
                       "https webhooks are not supported on a classic ESP32: use http://")

def _is_https(url):
	return str(url or "").strip().lower().startswith("https://")

# Reason phrases for the status codes the agent sends.
_STATUS_TEXT = {
	200: "OK", 400: "Bad Request", 403: "Forbidden",
	404: "Not Found", 422: "Unprocessable Entity", 500: "Internal Server Error",
}

# Sensors run scripts from this folder only (see _sensor_script_parts).
_SCRIPTS_DIR = const("scripts")
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

# A usable api_key is non-empty text (a number counts, as text). Anything else, empty,
# null or another type, counts as no key: the default on load, the current key on
# /config/load, ignored on /config/update.
def _valid_key(v):
	v = _as_text(v)
	return v if isinstance(v, str) and v else None

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
	return None, _SCRIPTS_DIR + "/" + parts[0], parts[1:]

# /script/* (list, read, write, run) over _SCRIPTS_DIR. A name is a sensor script name
# without a leading "-", up to _SCRIPT_NAME_MAX characters. Writes are refused (403) on the
# default api_key and while write.lock exists; a write may create the lock but not change or
# remove it. Runs are not locked. write.lock, in any letter case, is never run.
_SCRIPT_LOCK_FILE = const("write.lock")
_SCRIPT_NAME_MAX  = const(32)
_SCRIPT_OUTPUT_CAP = const(2048) # max chars of output a run returns

class ScriptError(Exception):
	def __init__(self, status, message):
		super().__init__(message)
		self.status = status

def _stat(path):
	try:
		return os.stat(path)
	except OSError:
		return None

# ujson decodes an escaped pair such as \ud83c\udf21 as two characters; join each pair into one.
def _join_surrogates(s):
	if not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
		return s
	out, hi = [], 0
	for ch in s:
		c = ord(ch)
		if hi and 0xDC00 <= c <= 0xDFFF:
			out.append(chr(0x10000 + ((hi - 0xD800) << 10) + c - 0xDC00))
			hi = 0
			continue
		if hi:
			out.append(chr(hi))
			hi = 0
		if 0xD800 <= c <= 0xDBFF:
			hi = c
		else:
			out.append(ch)
	if hi:
		out.append(chr(hi))
	return "".join(out)

# Returns (path, stat or None) for a valid name; a subfolder is refused.
def _script_path(name):
	if not _is_script_name(name) or name[0] == "-":
		raise ScriptError(400, "script must be a bare filename in the scripts folder")
	if len(name) > _SCRIPT_NAME_MAX:
		raise ScriptError(400, f"script name over {_SCRIPT_NAME_MAX} characters")
	path = _SCRIPTS_DIR + "/" + name
	st = _stat(path)
	if st and st[0] & 0x4000:
		raise ScriptError(400, "not a regular file")
	return path, st

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
				self._timer = machine.Timer(_LED_TIMER_ID)
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
		self._sock  = None
		self._url   = None
		self._last  = 0
		self._addrs = {}  # (host, port) -> address: DNS has no timeout, so each host is looked up once

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
		key = (host, port)
		addr = self._addrs.get(key)
		if addr is None:
			addr = self._addrs[key] = usocket.getaddrinfo(host, port)[0][-1]
		gc.collect()
		s = usocket.socket()
		s.settimeout(3)
		try:
			s.connect(addr)
			if proto == "https":
				gc.collect()
				ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
				ctx.verify_mode = ssl.CERT_NONE
				s = ctx.wrap_socket(s, server_hostname=host)
			return s, host, "/" + path
		except Exception:
			self._addrs.pop(key, None)  # look it up again next time, in case it moved
			try:
				s.close()
			except Exception:
				pass
			raise

	# True when the cached https socket has been idle longer than _WEBHOOK_IDLE_MS.
	def _expired(self):
		return self._sock is not None and utime.ticks_diff(utime.ticks_ms(), self._last) > _WEBHOOK_IDLE_MS

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
			reused = self._sock is not None and self._url == url and not self._expired()
			try:
				if not reused:
					self._close()
					self._sock, self._host, self._path = self._connect(url)
					self._url = url
				status, keep = self._request(self._sock, self._host, self._path, body, api_key)
				if not keep:
					self._close()
				self._last = utime.ticks_ms()
			except Exception:
				self._close()
				# Only a reused socket gets a second try now: the server may simply have closed
				# it while idle, which fails fast. A fresh connection that failed is left to the
				# retry queue, so one slow or unreachable target never holds the server twice.
				if attempt or not reused:
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
		# Agent-block fields that don't change while the agent runs, read once (see _agent_info).
		self._os          = self._get_os()
		self._serial_hex  = self._serial()
		self._mac         = ""
		self._ssid        = ""
		try:
			self._config_time = self._config_stamp(os.stat(_CONFIG_FILE)[8])
		except Exception:
			self._config_time = ""
		self._load_config()
		self._load_status()
		self._initialize_pins()
		self.log("[BOOT] GPIO manager ready")

	# Loads config.json (config-backup.json when it fails to parse), fills in defaults,
	# and drops what can't apply: pins outside this chip's table, a pullup on a
	# non-input, watched:false, and an invalid init.
	def _load_config(self):
		try:
			with open(_CONFIG_FILE) as f:
				self.config = ujson.load(f)
		except OSError:
			self.config = {}
		except Exception as e:
			self.log(f"[Config] {_CONFIG_FILE} is invalid ({e}); loading backup {_BACKUP_FILE}")
			try:
				with open(_BACKUP_FILE) as f:
					self.config = ujson.load(f)
			except Exception:
				self.config = {}
		self.config["api_key"] = _valid_key(self.config.get("api_key")) or _DEFAULT_API_KEY
		self.config.setdefault("gpios",         {})
		self.config.setdefault("notifications", {})
		self.config.setdefault("bridges",       {})
		self.config.setdefault("settings",      {})
		self.config.setdefault("sensors",       {})
		self.config["settings"].setdefault("logs_enabled", True)
		self.config["settings"].setdefault("log_days", _DEFAULT_LOG_DAYS)
		try:
			self.config["settings"]["log_days"] = max(0, min(int(self.config["settings"]["log_days"]), 30))
		except (TypeError, ValueError):
			self.config["settings"]["log_days"] = _DEFAULT_LOG_DAYS
		for g in list(self.config["gpios"]):
			if not g.isdigit() or int(g) not in GPIO_PINS:
				del self.config["gpios"][g]
				self.log(f"[Config] GP{g} dropped (not usable)")
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
			with open(_CONFIG_FILE, "w") as f:
				ujson.dump(self.config, f)
			self._config_time = self._config_stamp()
		except Exception:
			self._config_dirty = True

	def _flush_status(self):
		if not self._status_dirty:
			return
		self._status_dirty = False
		try:
			with open(_STATUS_FILE, "w") as f:
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
			with open(_CONFIG_FILE) as src:
				data = src.read()
		except OSError:
			return
		with open(_BACKUP_FILE, "w") as dst:
			dst.write(data)

	# Each pin's last saved logical level, for init "last".
	def _load_status(self):
		try:
			with open(_STATUS_FILE) as f:
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

	# At most one webhook per pin every _WEBHOOK_MIN_MS (debounce).
	def _on_watch_trigger(self, gpio_str):
		now  = utime.ticks_ms()
		last = self._last_watch_ms.get(gpio_str)
		if last is not None and utime.ticks_diff(now, last) < _WEBHOOK_MIN_MS:
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

				if probe:
					machine.Pin(gpio, machine.Pin.IN, None)

				# `value` equals `level` unless the pin is reversed, so both are sent only then.
				entry = {"gpio": gpio, "level": level, "type": pin_type}
				if conf.get("name"):
					entry["name"] = conf["name"]
				if pin_type == "input" and conf.get("pullup"):
					entry["pullup"] = conf["pullup"]
				if reversed_flag:
					entry["value"]    = 1 - level
					entry["reversed"] = True
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
		serial = self._serial_hex
		return ("esp32-" + serial[-4:]) if serial else "esp32"

	# Live, non-persisted facts about the machine this agent runs on. Returned under
	# `agent` by /config/read and /gpio/scan, and never stored in config.json - which
	# is why _normalize_config() drops it from anything posted back to /config/*.
	# What can't change while the agent runs (os, serial, mac, the connection's SSID)
	# is read once; config_updated is set whenever the agent writes config.json.
	def _agent_info(self):
		wlan = network.WLAN(network.STA_IF)
		connected = wlan.isconnected()
		ip = wlan.ifconfig()[0] if connected else "unknown"
		signal = None
		if connected:
			if not self._ssid:
				self._ssid = self._read_ssid(wlan)
			try:
				signal = wlan.status("rssi")
			except Exception:
				signal = None
		else:
			self._ssid = ""  # read again on the next connection
		if not self._mac:
			try:
				self._mac = ":".join("{:02x}".format(b) for b in wlan.config("mac"))
			except Exception:
				self._mac = ""
		return {"platform": PLATFORM,
		        "os": self._os,
		        "host": "esp32",
		        "serial": self._serial_hex,
		        "ip": ip,
		        "mac": self._mac,
		        "wifi": self._ssid,
		        "signal": signal,
		        "cpu_temperature": self._cpu_temperature(),
		        "uptime": _format_uptime(utime.time() - _BOOT_TIME),
		        "version": VERSION,
		        "config_updated": self._config_time}

	# The SSID of the current connection, or "" when the chip can't say.
	def _read_ssid(self, wlan):
		for field in ("essid", "ssid"):
			try:
				ssid = wlan.config(field) or ""
				return ssid.decode() if isinstance(ssid, bytes) else ssid
			except Exception:
				continue
		return ""

	# config_updated: when config.json was last written (seconds since the epoch, or now), as
	# "YYYY-MM-DD HH:MM:SS", or "" before NTP has set the clock.
	def _config_stamp(self, secs=None):
		try:
			t = utime.localtime() if secs is None else utime.localtime(secs)
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
				"log_days":     settings.get("log_days", _DEFAULT_LOG_DAYS),
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
			gc.collect()  # a script's own allocations need the room (a 5 KB string failed without)
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

	# "locked" (write.lock present), "default_key" (api_key unchanged) or "enabled".
	def _script_write_state(self):
		if _stat(_SCRIPTS_DIR + "/" + _SCRIPT_LOCK_FILE):
			return "locked"
		if self.config.get("api_key", _DEFAULT_API_KEY) == _DEFAULT_API_KEY:
			return "default_key"
		return "enabled"

	# GET /script/list: every file in scripts/ (no subfolders), plus the write state.
	def list_scripts(self):
		try:
			names = sorted(os.listdir(_SCRIPTS_DIR))
		except OSError:
			names = []
		out = []
		for n in names:
			st = _stat(_SCRIPTS_DIR + "/" + n)
			if st and not st[0] & 0x4000 and _is_script_name(n) and n[0] != "-":
				t = utime.localtime(st[8])
				out.append({"name": n, "size": st[6],
				            "modified": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*t[:6]) if t[0] >= 2023 else ""})
		return {"scripts": out, "write": self._script_write_state()}

	# GET /script/read/{name}.
	def read_script(self, name):
		path, st = _script_path(name)
		if not st:
			raise ScriptError(404, f"Script not found: {name}")
		with open(path) as f:
			return {"name": name, "content": f.read()}

	# POST /script/write/{name}: {"content": "..."} creates or overwrites, {"remove": true}
	# deletes. The text goes to a hidden temp file that is then renamed into place, so a
	# run never sees half a script.
	def write_script(self, name, body):
		path, st = _script_path(name)
		state = self._script_write_state()
		if state == "locked":
			raise ScriptError(403, "script writes are locked (write.lock in scripts/)")
		if state == "default_key":
			raise ScriptError(403, "script writes need an api_key other than the default")
		if body.get("remove"):
			if st:
				os.remove(path)
				self.log(f"[Script] {name} removed")
			return {"name": name, "status": "removed" if st else "not found"}
		content = _as_text(body.get("content"))
		if content is None:
			raise ScriptError(400, "Send 'content' (text), or 'remove': true to delete the script")
		if not isinstance(content, str):
			raise ScriptError(422, "Request body failed validation")
		content = _join_surrogates(content)
		try:
			os.mkdir(_SCRIPTS_DIR)
		except OSError:
			pass
		tmp = _SCRIPTS_DIR + "/." + name + ".tmp"
		with open(tmp, "w") as f:
			f.write(content)
		try:
			os.rename(tmp, path)
		except OSError:
			_rm(path)   # a filesystem that won't rename over an existing file
			os.rename(tmp, path)
		size = os.stat(path)[6]
		self.log(f"[Script] {name} written ({size} bytes)")
		return {"name": name, "size": size}

	# POST /script/run/{name}: exec'd in-process like a sensor. output is read(*args), or
	# `result`, as text (JSON for a non-string). A script that raises (SystemExit too) gives
	# exit_code 1 and "<Error>: <message>". No timeout.
	def run_script(self, name, body):
		path, st = _script_path(name)
		if name.lower() == _SCRIPT_LOCK_FILE:
			raise ScriptError(400, "write.lock is not a script")
		if not st:
			raise ScriptError(404, f"Script not found: {name}")
		args = body.get("args") or []
		if not isinstance(args, list):
			raise ScriptError(422, "Request body failed validation")
		args = [_as_text(a) for a in args]
		if any(not isinstance(a, str) for a in args):
			raise ScriptError(422, "Request body failed validation")
		if any(not _is_alnum(a) for a in args):
			raise ScriptError(400, "script args must be letters and digits only")
		with open(path) as f:
			src = f.read()
		code = 0
		try:
			gc.collect()  # a script's own allocations need the room (a 5 KB string failed without)
			ns = {"args": args}
			exec(src, ns)
			val = ns["read"](*args) if callable(ns.get("read")) else ns.get("result")
		except (Exception, SystemExit) as e:
			code, val = 1, f"{type(e).__name__}: {e}"
		out = "" if val is None else val if isinstance(val, str) else ujson.dumps(val)
		self.log(f"[Script] {name} run, exit code {code}")
		return {"name": name, "exit_code": code, "output": out[:_SCRIPT_OUTPUT_CAP].rstrip()}

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
				if _valid_key(v):
					self.config["api_key"] = _valid_key(v)
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
			self.log(f"[Config] https webhook dropped: {notif.get('webhook')}")
			notif["webhook"] = None
		new_config["api_key"] = _valid_key(new_config.get("api_key")) or self.config.get("api_key", _DEFAULT_API_KEY)
		new_config.setdefault("gpios", {})
		new_config.setdefault("sensors", {})
		new_config.setdefault("settings", {})
		new_config["settings"].setdefault("log_days", _DEFAULT_LOG_DAYS)
		with open(_CONFIG_FILE, "w") as f:
			ujson.dump(new_config, f)
		self._config_time = self._config_stamp()
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
		if len(self._log) > _LOG_MAX:
			self._log.pop(0)

	# GET /logs: the newest _LOG_RETURN lines.
	def get_logs(self):
		return self._log[-_LOG_RETURN:]

	def clear_logs(self):
		self._log.clear()

	# Hourly from the main loop: drops lines older than log_days (an hour for 0).
	def maybe_cleanup_logs(self):
		now = utime.time()
		if now - self._last_log_cleanup < 3600:
			return
		self._last_log_cleanup = now
		days   = self.config.get("settings", {}).get("log_days", _DEFAULT_LOG_DAYS)
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
	# one is queued again _WEBHOOK_RETRY_MS later, up to _WEBHOOK_RETRIES times.
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
			if attempts < _WEBHOOK_RETRIES:
				self._webhook_queue.append((gpio_str, attempts + 1, url, api_key,
				                            utime.ticks_add(utime.ticks_ms(), _WEBHOOK_RETRY_MS)))

# Resets

# Every reset the agent makes itself goes through here: it first writes the reason to
# last-reset.txt, which the next boot logs (_log_reset_cause), since the log itself is in
# memory and lost with the reset. soft=True keeps the Wi-Fi link (see /upgrade).
def _reset(reason, soft=False):
	try:
		t = utime.localtime()
		with open(_RESET_NOTE, "w") as f:
			f.write("{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} {}".format(*t[:6], reason))
	except Exception:
		pass
	if soft:
		machine.soft_reset()
	else:
		machine.reset()

_RESET_CAUSES = (("PWRON_RESET", "power on"), ("HARD_RESET", "hard reset"), ("WDT_RESET", "watchdog"),
                 ("DEEPSLEEP_RESET", "deep sleep"), ("SOFT_RESET", "soft reset"))

# Logs why the board started: machine.reset_cause(), plus the agent's own note when it
# reset itself.
def _log_reset_cause(manager):
	try:
		cause = machine.reset_cause()
		cause = next((text for name, text in _RESET_CAUSES if getattr(machine, name, None) == cause), str(cause))
	except Exception:
		cause = "unknown"
	try:
		with open(_RESET_NOTE) as f:
			note = f.read().strip()
		_rm(_RESET_NOTE)
	except OSError:
		note = ""
	manager.log(f"[BOOT] Reset cause: {cause}" + (f"; the agent reset itself at {note}" if note else ""))

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
	ssid     = settings.get("SSID")     or _FALLBACK_SSID
	password = settings.get("PASSWORD") or _FALLBACK_PASSWORD

	if not ssid:
		print("[Wi-Fi] No SSID in wifi.txt")
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
		print(f"[Wi-Fi] Failed ({status_desc}), resetting")
		_reset(f"Wi-Fi connect failed ({status_desc})")

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
	return utime.localtime()[0] >= 2023

# Set again in main() once the clock is synced.
_BOOT_TIME = utime.time()

# One attempt at NTP, from main() every _NTP_RETRY_MS while the clock is unset (a single
# try at boot can be lost on a weak link, and the clock would then read 2000 until the
# next reboot). On success the clock jumps, so _BOOT_TIME moves with it and uptime stays
# right. Returns when to try next, or None once the clock is set.
def _retry_ntp(manager):
	global _BOOT_TIME
	before = utime.time()
	if not sync_time():
		return utime.ticks_ms()
	_BOOT_TIME += utime.time() - before
	manager.log("[BOOT] Clock set over NTP")
	return None

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
		wlan.connect(settings.get("SSID") or _FALLBACK_SSID, settings.get("PASSWORD") or _FALLBACK_PASSWORD)
	except OSError:
		pass

class WifiWatchdog:
	"""Recovers a link that reports connected but passes nothing. Only while idle: probe the gateway every
	_WIFI_PROBE_SECS, reconnect softly after two failed probes (GPIO untouched), and
	reset only if the link is still dead _WIFI_RECONNECT_SECS later. A link that
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
		_reset("Wi-Fi watchdog: gateway still unreachable after a reconnect")

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
			if now - self._reconnect_at >= _WIFI_RECONNECT_SECS:
				self._log("still unreachable, resetting")
				self._reset()
			return
		# busy: a pulse revert is pending, and a probe can block for up to 2 s.
		if busy or not self._wlan.isconnected():
			return
		if now - self._last_ok < _WIFI_PROBE_SECS or now - self._last_probe < _WIFI_PROBE_SECS:
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
	reply, and nothing else resets it. Every _HEAP_CHECK_SECS this reads the largest free
	block; below _HEAP_LOW_BYTES for _HEAP_LOW_SECS, with no pulse pending, it flushes and
	resets so the board comes back clean. Never within _HEAP_BOOT_GRACE_SECS of its start
	(main() creates it at boot): an ESP32-C3 measured 5.6 KB in the first minutes after boot
	and 20-24 KB once settled. A low stretch that began during the grace still counts, so a
	board still low when the grace ends is reset then."""

	def __init__(self, manager, largest=_largest_free_block, reset=None, now=utime.time):
		self._manager, self._largest, self._now = manager, largest, now
		self._reset = reset or self._hard_reset
		self._checked = self._started = now()
		self._low_since = None

	def _log(self, msg):
		self._manager.log("[HEAP] " + msg)
		print("[HEAP] " + msg)

	def _hard_reset(self):
		self._manager._flush_all()
		_reset(f"heap watchdog: largest free block under {_HEAP_LOW_BYTES} bytes")

	def tick(self, busy=False):
		now = self._now()
		if now - self._checked < _HEAP_CHECK_SECS:
			return
		self._checked = now
		largest = self._largest()
		if largest is None or largest >= _HEAP_LOW_BYTES:
			if largest is not None and self._low_since is not None:
				self._log("recovered (largest block %d B)" % largest)
			self._low_since = None
			return
		if self._low_since is None:
			self._low_since = now
			self._log("largest block %d B, under %d" % (largest, _HEAP_LOW_BYTES))
		elif now - self._low_since >= _HEAP_LOW_SECS and not busy and now - self._started >= _HEAP_BOOT_GRACE_SECS:
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
			if len(data) > _MAX_REQUEST_BYTES:
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
			# lwIP's buffers and took a classic ESP32 off the network (api-tester test 53).
			# The Pi answers 413 after reading; a client may see a reset instead here.
			if chunked or cl > _MAX_REQUEST_BYTES:
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
			except MemoryError:
				raise   # out of heap, not bad JSON: handle_request answers 500, not 422
			except Exception:
				body = None
		return method, path, hdrs, body
	except MemoryError:
		raise
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

# Sends a JSON response in one write: one ujson.dumps for the body, sent together with its
# header, so the client never waits on a delayed ACK between pieces. Every response closes
# the connection (no keep-alive).
def _send_json(conn, data, status=200):
	body = ujson.dumps(data).encode()
	try:
		_send_all(conn, (
			f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\n"
			f"Content-Type: application/json\r\n"
			f"Content-Length: {len(body)}\r\n"
			f"Connection: close\r\n\r\n"
		).encode() + body)
	except Exception:
		pass

# Upgrade

# Removes a file if it exists.
def _rm(path):
	try:
		os.remove(path)
	except Exception:
		pass

# POST /upgrade: leaves upgrade.lock for main.py and lets the caller soft-reset (the
# Wi-Fi link stays up), so the download runs in upgrader.mpy on a fresh heap. A board
# installed without upgrader.mpy answers 500 and is upgraded over USB.
def _do_upgrade(manager):
	try:
		os.stat("upgrader.mpy")
	except OSError:
		return (False, {"error": "upgrader.mpy missing: install over USB"}, 500)
	try:
		with open(_UPGRADE_LOCK, "w") as f:
			ujson.dump({"url": UPGRADE_BASE_URL, "files": UPGRADE_FILES, "attempt": 0}, f)
	except Exception as e:
		_rm(_UPGRADE_LOCK)
		return (False, {"error": "could not write upgrade.lock: " + str(e)}, 500)
	manager.log("[UPGRADE] Restarting")
	return (True, {"status": "upgrading"}, 200)

# Logs what upgrader.mpy reported, once.
def _log_upgrade_result(manager):
	try:
		with open(_UPGRADE_RESULT) as f:
			result = ujson.load(f)
	except Exception:
		return
	_rm(_UPGRADE_RESULT)
	if result.get("ok"):
		manager.log(f"[UPGRADE] Done, now {VERSION}: {result.get('message', '')}")
	else:
		manager.log(f"[UPGRADE] Failed, still {VERSION}: {result.get('message', '')}")

# Requests

# Serves one request and always closes the socket. Running out of memory answers
# 500 instead of leaving the client waiting.
def handle_request(conn, manager):
	BLINKER.blink(2)
	try:
		_handle_request(conn, manager)
	except MemoryError:
		gc.collect()
		manager.log("[ERROR] Out of memory")
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

		key = hdrs.get("api-key")
		if not key or key != manager.config.get("api_key", _DEFAULT_API_KEY):
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
		elif method == "GET"  and path == "/script/list":
			_send_json(conn, manager.list_scripts())
		elif method == "GET"  and path.startswith("/script/read/"):
			_send_json(conn, manager.read_script(path[len("/script/read/"):]))
		elif method == "POST" and path.startswith("/script/write/"):
			_send_json(conn, manager.write_script(path[len("/script/write/"):], body))
		elif method == "POST" and path.startswith("/script/run/"):
			_send_json(conn, manager.run_script(path[len("/script/run/"):], body))
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
				_reset("POST /upgrade", soft=True)
		# reboot: true resets the board; otherwise pins and config reload in place.
		elif method == "POST" and path == "/restart":
			reboot = bool(body.get("reboot", False))
			_send_json(conn, {"status": "rebooting" if reboot else "restarting"})
			conn.close()
			manager._flush_all()
			if reboot:
				utime.sleep_ms(500)
				try:
					_reset("POST /restart (reboot)")
				except Exception as e:
					manager.log(f"[CONFIG] Reboot failed: {e}; falling back to soft restart")
					try:
						machine.soft_reset()
					except Exception:
						_rm(_RESET_NOTE)  # no reset after all
						manager._reload()
						manager.log("[BOOT] In-process restart via /restart")
			else:
				manager._reload()
				manager.log("[BOOT] In-process restart via /restart")
		elif method == "GET"  and path == "/logs":
			_send_json(conn, {"logs": manager.get_logs()})
		else:
			_send_empty(conn, 404)

	except ScriptError as e:
		if e.status == 404:
			_send_empty(conn, 404)
		else:
			_send_json(conn, {"error": str(e)}, e.status)
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
	if hasattr(gc, "threshold"):  # MicroPython only; the CPython unit tests have none
		gc.threshold(_GC_THRESHOLD)
	BLINKER.start_fast()
	ip      = connect_wifi()
	ntp_retry = None if sync_time() else utime.ticks_ms()
	global _BOOT_TIME
	_BOOT_TIME = utime.time()
	manager = GPIOManager()
	_log_upgrade_result(manager)
	_log_reset_cause(manager)
	manager.queue_boot_webhook()

	addr   = usocket.getaddrinfo("0.0.0.0", _PORT)[0][-1]
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
					_reset("port 8314 in use")
				manager.log(f"[BOOT] Port in use, retrying ({attempt+1}/2)...")
				utime.sleep(2)
			else:
				raise
	server.listen(3)
	server.settimeout(1)

	manager.log(f"[BOOT] Listening on {ip}:{_PORT}")
	BLINKER.stop_fast()  # booting is over: Wi-Fi is up and the server answers
	print(f"[BOOT] Listening on {ip}:{_PORT}")

	wlan         = network.WLAN(network.STA_IF)
	wifi_last_ok = utime.time()
	watchdog     = WifiWatchdog(wlan, manager)
	heap_watch   = HeapWatch(manager)

	while True:
		try:
			manager.maybe_cleanup_logs()
			manager.maybe_flush()
			manager.process_reverts()
			manager.process_webhooks()
			manager._webhook_sender.maybe_expire()
			if ntp_retry is not None and utime.ticks_diff(utime.ticks_ms(), ntp_retry) >= _NTP_RETRY_MS:
				ntp_retry = _retry_ntp(manager)
			server.settimeout(0.05 if manager._pending_reverts else 1)
			now = utime.time()
			# Dead-man switch: reset when Wi-Fi has been down for _WIFI_DEAD_SECS.
			if wlan.isconnected():
				wifi_last_ok = now
			elif now - wifi_last_ok > _WIFI_DEAD_SECS:
				manager._flush_all()
				_reset(f"Wi-Fi down for over {_WIFI_DEAD_SECS} s")
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
