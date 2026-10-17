import time as _time

time = _time.time
sleep = _time.sleep
localtime = _time.localtime

def sleep_ms(ms):
    _time.sleep(ms / 1000.0)

def ticks_ms():
    return int(_time.monotonic() * 1000)

def ticks_add(t, delta):
    return (t + delta) & 0x3fffffff

def ticks_diff(t1, t2):
    diff = (t1 - t2) & 0x3fffffff
    if diff >= 0x20000000:
        diff -= 0x40000000
    return diff
