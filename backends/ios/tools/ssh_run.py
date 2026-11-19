"""Run a shell command on the phone over SSH and print output.

  python backends/ios/tools/ssh_run.py <iphone-ip> "uname -a"
  python backends/ios/tools/ssh_run.py <iphone-ip> --put local.bin /var/mobile/x.bin
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import paramiko


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("cmd", nargs="?")
    ap.add_argument("--user", default="mobile")
    ap.add_argument("--password", default=os.environ.get("BLE2REMOTE_SSH_PASSWORD"),
                    help="SSH password (default: $BLE2REMOTE_SSH_PASSWORD, else prompt)")
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    if not a.password:
        a.password = getpass.getpass(f"{a.user}@{a.host} password: ")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=a.user, password=a.password, look_for_keys=False, allow_agent=False)
    if a.put or a.get:
        sftp = c.open_sftp()
        if a.put:
            sftp.put(*a.put)
            print(f"put {a.put[0]} -> {a.put[1]}")
        if a.get:
            sftp.get(*a.get)
            print(f"get {a.get[0]} -> {a.get[1]}")
    rc = 0
    if a.cmd:
        _, out, err = c.exec_command(a.cmd, timeout=a.timeout)
        sys.stdout.write(out.read().decode("utf-8", "replace"))
        sys.stderr.write(err.read().decode("utf-8", "replace"))
        rc = out.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
