"""Startet uvicorn auf einem dual-stack Socket.

uvicorn bindet mit --host :: ueber asyncio, und asyncio setzt dabei
IPV6_V6ONLY — der Dienst waere IPv6-only und der published IPv4-Port des
Containers wuerde nichts mehr tragen. Hier wird der Socket deshalb selbst
gebaut (V6ONLY=0, nimmt also auch IPv4-mapped Verbindungen an) und uvicorn
per fd uebergeben.
"""

import os
import socket

import uvicorn

from main import app

HOST = os.getenv("HOST", "::")
PORT = int(os.getenv("PORT", "8000"))


def dual_stack_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, port))
    sock.listen(128)
    return sock


if __name__ == "__main__":
    # Referenz halten: inline wuerde der GC das Socket-Objekt einsammeln und den
    # fd schliessen -> uvicorn scheitert mit "Socket operation on non-socket".
    sock = dual_stack_socket(HOST, PORT)
    uvicorn.run(app, fd=sock.fileno())
