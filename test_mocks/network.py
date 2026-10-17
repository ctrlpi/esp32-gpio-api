STA_IF = 0
AP_IF = 1

class WLAN:
    STA_IF = 0
    AP_IF = 1

    def __init__(self, interface=STA_IF):
        self.interface = interface
        self._active = True
        self._connected = True

    def active(self, is_active=None):
        if is_active is not None:
            self._active = is_active
        return self._active

    def connect(self, ssid, password):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isconnected(self):
        return self._connected

    def ifconfig(self, config=None):
        return ("127.0.0.1", "255.255.255.0", "127.0.0.1", "8.8.8.8")

    def config(self, param):
        if param == "mac":
            return b"\x28\xcd\xc1\x12\x34\x56"
        elif param in ("ssid", "essid"):
            return "TestWiFi"
        return ""

    def status(self, param=None):
        if param == "rssi":
            return -55
        return 1010  # STAT_GOT_IP

    def scan(self):
        return []
