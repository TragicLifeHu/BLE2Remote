"""Deploy the iPhone backend and the control plane to the phone over SSH and run the installer.

  python backends/ios/tools/deploy.py <iphone-ip> [--password PW] [--no-install] [-- --pipe-lan]
Uploads (minus caches) to /var/mobile/ble2remote, then runs `sudo sh install.sh`:
  backends/ios/   -> /var/mobile/ble2remote/       (blepipe.py, install.sh, entitlements)
  control_plane/  -> /var/mobile/ble2remote/app/
"""
from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import sys

import paramiko

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
REMOTE = "/var/mobile/ble2remote"
SOURCES = [  # (local dir, remote dir) -- the phone-side layout install.sh expects
    (os.path.join(REPO, "backends", "ios"), REMOTE),
    (os.path.join(REPO, "control_plane"), REMOTE + "/app"),
]
SKIP_DIRS = {"__pycache__", "data", "logs", "lib", "tools"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--user", default="mobile")
    ap.add_argument("--password", default=os.environ.get("BLE2REMOTE_SSH_PASSWORD"),
                    help="SSH password (default: $BLE2REMOTE_SSH_PASSWORD, else prompt)")
    ap.add_argument("--no-install", action="store_true", help="upload only, then restart the app daemon")
    ap.add_argument("install_args", nargs="*")
    a = ap.parse_args()
    if not a.password:
        a.password = getpass.getpass(f"{a.user}@{a.host} password: ")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=a.user, password=a.password, look_for_keys=False, allow_agent=False)
    sftp = c.open_sftp()

    def mkdirs(path: str) -> None:
        parts = path.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except OSError:
                sftp.mkdir(cur)

    n = 0
    for root, rroot in SOURCES:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel = os.path.relpath(dirpath, root).replace("\\", "/")
            rdir = rroot if rel == "." else posixpath.join(rroot, rel)
            mkdirs(rdir)
            for fn in filenames:
                if fn.endswith((".pyc", ".tmp")):
                    continue
                local, remote = os.path.join(dirpath, fn), posixpath.join(rdir, fn)
                if fn.endswith((".sh", ".py", ".plist", ".entitlements")):
                    # never ship Windows line endings to the phone (a CRLF shell script fails there)
                    with open(local, "rb") as f:
                        data = f.read().replace(b"\r\n", b"\n")
                    with sftp.open(remote, "wb") as f:
                        f.write(data)
                else:
                    sftp.put(local, remote)
                n += 1
    print(f"uploaded {n} files to {REMOTE}")

    def run(cmd: str) -> int:
        _, out, err = c.exec_command(cmd, timeout=180)
        sys.stdout.write(out.read().decode("utf-8", "replace"))
        sys.stdout.write(err.read().decode("utf-8", "replace").replace("[sudo] password for mobile: ", ""))
        return out.channel.recv_exit_status()

    sudo = f"echo '{a.password}' | sudo -S "
    if a.no_install:
        rc = run(sudo + "launchctl kickstart -k system/com.ble2remote.app")
    else:
        rc = run(f"cd {REMOTE} && {sudo}sh install.sh {' '.join(a.install_args)}")
    c.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
