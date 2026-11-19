"""Forward local TCP port -> phone's 127.0.0.1:<port> over SSH (frida-server tunnel).

  python backends/ios/tools/ssh_tunnel.py <iphone-ip> --user mobile --port 27042
Then:  frida-ps -H 127.0.0.1:27042
"""
from __future__ import annotations

import argparse
import getpass
import os
import select
import socket
import socketserver
import threading

import paramiko


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--user", default="mobile")
    ap.add_argument("--password", default=os.environ.get("BLE2REMOTE_SSH_PASSWORD"),
                    help="SSH password (default: $BLE2REMOTE_SSH_PASSWORD, else prompt)")
    ap.add_argument("--port", type=int, default=27042)
    ap.add_argument("--local-port", type=int, default=None)
    a = ap.parse_args()
    if not a.password:
        a.password = getpass.getpass(f"{a.user}@{a.host} password: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(a.host, username=a.user, password=a.password, look_for_keys=False, allow_agent=False)
    transport = client.get_transport()
    transport.set_keepalive(15)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            chan = transport.open_channel("direct-tcpip", ("127.0.0.1", a.port), self.request.getpeername())
            try:
                while True:
                    r, _, _ = select.select([self.request, chan], [], [])
                    if self.request in r:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in r:
                        data = chan.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
            finally:
                chan.close()
                self.request.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    lp = a.local_port or a.port
    srv = Server(("127.0.0.1", lp), Handler)
    print(f"tunnel 127.0.0.1:{lp} -> {a.host}:127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
