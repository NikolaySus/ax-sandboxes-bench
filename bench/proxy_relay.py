"""TCP relay from the private kind bridge to a host-loopback HTTP proxy."""

import select
import socket
import socketserver
import sys


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=20) as upstream:
                peers = [self.request, upstream]
                while True:
                    ready, _, _ = select.select(peers, [], [], 60)
                    if not ready:
                        return
                    for src in ready:
                        data = src.recv(65536)
                        if not data:
                            return
                        (upstream if src is self.request else self.request).sendall(data)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server((sys.argv[1], 18081), Relay).serve_forever()
