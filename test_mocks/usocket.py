import os
import socket as _socket
from socket import *

_orig_getaddrinfo = _socket.getaddrinfo

def getaddrinfo(host, port, *args, **kwargs):
    override = os.environ.get("ESP32_PORT") or os.environ.get("PICO_PORT")
    if override:
        port = int(override)
    return _orig_getaddrinfo(host, port, *args, **kwargs)

_orig_socket = _socket.socket

class socket(_orig_socket):
    def bind(self, addr):
        override = os.environ.get("ESP32_PORT") or os.environ.get("PICO_PORT")
        if override:
            addr = (addr[0], int(override))
        return super().bind(addr)
