# Reserve Wi-Fi memory before loading the application.

import network
import time


def _wifi_settings(path="wifi.txt"):
	settings = {}
	try:
		with open(path) as source:
			for line in source:
				line = line.strip()
				if not line or line.startswith("#") or "=" not in line:
					continue
				key, _, value = line.partition("=")
				settings[key.strip()] = value.strip().strip('"').strip("'")
	except OSError:
		pass
	return settings


settings = _wifi_settings()
ssid = settings.get("SSID")
password = settings.get("PASSWORD")

if ssid:
	print(f"[boot] Initializing Wi-Fi for '{ssid}'...")
	wlan = network.WLAN(network.STA_IF)
	if not wlan.active():
		wlan.active(True)
	if not wlan.isconnected():
		try:
			wlan.connect(ssid, password or "")
		except OSError:
			pass
else:
	print("[boot] No SSID found in wifi.txt")

del settings, ssid, password

