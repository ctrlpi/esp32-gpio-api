# upgrader.py - the over-the-air upgrade. POST /upgrade writes upgrade.lock and resets;
# main.py then imports this instead of app, so the TLS download gets a fresh heap that a
# running agent no longer has. build.sh compiles it to upgrader.mpy.
#
# upgrade.lock (JSON, written by app.py): url (the release folder), files ([name, kind,
# minimum size], ...) and attempt (raised here before each try). Every file is downloaded
# as <name>.new and checked; only when all are good are they swapped in, keeping
# <name>.backup, which is put back if the swap fails. A failed try resets and runs again,
# up to MAX_ATTEMPTS; then the lock is removed and the old agent starts. The outcome goes
# to upgrade-result.json, which the agent logs on its next boot.

import gc
import machine
import network
import os
import usocket
import ujson
import utime

try:
	import ssl
except ImportError:
	ssl = None

LOCK_FILE    = "upgrade.lock"
RESULT_FILE  = "upgrade-result.json"
MAX_ATTEMPTS = 3
WIFI_SECS    = 30
USER_AGENT   = "ctrlpi-esp32"

def _rm(path):
	try:
		os.remove(path)
	except Exception:
		pass

def _exists(path):
	try:
		os.stat(path)
		return True
	except OSError:
		return False

def _save(path, data):
	with open(path, "w") as f:
		ujson.dump(data, f)

# boot.py has usually started the connect already; this waits for it, or starts it.
def _wifi():
	wlan = network.WLAN(network.STA_IF)
	wlan.active(True)
	if not wlan.isconnected():
		settings = {}
		try:
			with open("wifi.txt") as f:
				for line in f:
					line = line.strip()
					if "=" in line and not line.startswith("#"):
						key, _, val = line.partition("=")
						settings[key.strip()] = val.strip().strip('"').strip("'")
		except OSError:
			pass
		try:
			wlan.connect(settings.get("SSID", ""), settings.get("PASSWORD", ""))
		except OSError:
			pass
	for _ in range(WIFI_SECS * 10):
		if wlan.isconnected():
			return
		utime.sleep_ms(100)
	raise OSError("Wi-Fi not connected")

# None if the file looks right (a .mpy header, or Python text) and has the expected
# size, else the reason.
def _verify(path, size, kind):
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
		return "size mismatch (wrote %d, read back %d)" % (size, total)
	return None

# Downloads url to dest (http or https, certificate not verified) with a Content-Length,
# chunked or read-to-close body. Returns the byte count; raises on any error or short read.
def _download(url, dest):
	proto, rest = url.split("://", 1)
	host_port, _, path = rest.partition("/")
	host, _, port = host_port.partition(":")
	port = int(port) if port else (443 if proto == "https" else 80)
	if proto == "https" and not ssl:
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
		s.write(("GET /{} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {}\r\n"
		         "Accept: */*\r\nConnection: close\r\n\r\n").format(path, host, USER_AGENT).encode())
		line = s.readline()
		if not line or not line.startswith(b"HTTP/"):
			raise OSError("bad response")
		status = int(line.split(b" ")[1])
		if status != 200:
			raise OSError("HTTP %d" % status)
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

# Downloads and checks every file, then swaps them in. Raises with the reason on failure,
# leaving the installed files as they were.
def _upgrade(job):
	files = job["files"]
	name = ""
	try:
		for name, kind, minimum in files:
			n = _download(job["url"] + name, name + ".new")
			if n < minimum:
				raise OSError("too small (%d bytes)" % n)
			bad = _verify(name + ".new", n, kind)
			if bad:
				raise OSError("failed verification: " + bad)
	except Exception as e:
		for item in files:
			_rm(item[0] + ".new")
		raise OSError("%s: %s" % (name, e))

	installed = []
	try:
		for name, kind, minimum in files:
			n = os.stat(name + ".new")[6]
			_rm(name + ".backup")
			if _exists(name):
				os.rename(name, name + ".backup")
			installed.append(name)
			os.rename(name + ".new", name)
			bad = _verify(name, n, kind)
			if bad:
				raise OSError(name + ": " + bad)
	except Exception as e:
		for name in installed:
			if _exists(name + ".backup"):
				_rm(name)
				os.rename(name + ".backup", name)
		for item in files:
			_rm(item[0] + ".new")
		raise OSError("install failed, previous files restored: " + str(e))

# A reset in the middle of a swap can leave a file renamed to .backup with nothing in its
# place: put it back before anything else.
def _restore_missing(files):
	for item in files:
		name = item[0]
		if not _exists(name) and _exists(name + ".backup"):
			os.rename(name + ".backup", name)

def _finish(ok, message):
	try:
		_save(RESULT_FILE, {"ok": ok, "message": message})
	except Exception:
		pass
	_rm(LOCK_FILE)

def _run():
	with open(LOCK_FILE) as f:
		job = ujson.load(f)
	files = job["files"]
	_restore_missing(files)
	attempt = job.get("attempt", 0) + 1
	if attempt > MAX_ATTEMPTS:
		_finish(False, "gave up after %d attempts: %s" % (MAX_ATTEMPTS, job.get("error", "")))
		return True
	job["attempt"] = attempt
	_save(LOCK_FILE, job)
	try:
		_wifi()
		_upgrade(job)
	except Exception as e:
		print("[UPGRADE] attempt %d failed: %s" % (attempt, e))
		if attempt >= MAX_ATTEMPTS:
			_finish(False, "gave up after %d attempts: %s" % (attempt, e))
			return True
		job["error"] = str(e)
		_save(LOCK_FILE, job)
		return False
	message = ", ".join(item[0] for item in files) + " replaced (attempt %d)" % attempt
	if job.get("error"):
		message += ", after: " + job["error"]
	_finish(True, message)
	return True

# Always ends in a reset. Into the agent it is a soft reset, which keeps the Wi-Fi link and
# still gives the agent a fresh heap; a retry gets a hard reset, which starts the radio
# afresh. Anything unexpected ends the upgrade, so the board can never stop here.
def run():
	try:
		done = _run()
	except Exception as e:
		_finish(False, "upgrader error: " + str(e))
		done = True
	utime.sleep_ms(200)
	if done:
		machine.soft_reset()
	else:
		machine.reset()

run()
