from json import *
from json import loads as _loads


# MicroPython's ujson.loads takes any buffer (bytearray, memoryview); CPython's
# json.loads only takes str/bytes, so convert first.
def loads(s, *args, **kwargs):
    if isinstance(s, (bytearray, memoryview)):
        s = bytes(s)
    return _loads(s, *args, **kwargs)
