import sys
import os

try:
    class _MockUname:
        sysname = "esp32"
        nodename = "esp32"
        release = "1.24.0"
        version = "v1.24.0"
        machine = "ESP32 module with ESP32"
    os.uname = lambda: _MockUname()
except Exception:
    pass

class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 1
    PULL_DOWN = 2
    IRQ_RISING = 1
    IRQ_FALLING = 2

    _pins = {}

    def __init__(self, id, mode=IN, pull=None, value=None):
        self.id = id
        self.mode = mode
        self.pull = pull
        self.irq_handler = None
        if id not in Pin._pins:
            Pin._pins[id] = {"value": 0 if value is None else int(value), "mode": mode}
        elif value is not None:
            Pin._pins[id]["value"] = int(value)

    def value(self, v=None):
        if v is not None:
            Pin._pins[self.id]["value"] = int(bool(v))
        return Pin._pins[self.id]["value"]

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def toggle(self):
        self.value(1 - self.value())

    def irq(self, trigger=0, handler=None):
        self.irq_handler = handler

class Timer:
    PERIODIC = 1
    ONE_SHOT = 2

    def __init__(self, id=-1):
        self.id = id

    def init(self, period=1000, mode=PERIODIC, callback=None):
        self.callback = callback

    def deinit(self):
        pass

def unique_id():
    return b"\x12\x34\x56\x78\x9a\xbc"

PWRON_RESET = 1
HARD_RESET = 2
WDT_RESET = 3
DEEPSLEEP_RESET = 4
SOFT_RESET = 5

def reset_cause():
    return PWRON_RESET

def reset():
    sys.exit(0)

def soft_reset():
    sys.exit(0)
