"""Render iOS PWA launch images (apple-touch-startup-image) with headless Edge.

  python tools/make_splash.py

Writes control_plane/static/splash/<w>x<h>-<light|dark>.png for each iPhone screen size below and
prints the <link> tags to paste into index.html. iOS only uses a startup image whose media
query matches the device exactly, so every size needs its own file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "control_plane", "static")
OUT = os.path.join(STATIC, "splash")

# (CSS width, CSS height, pixel ratio) in portrait
DEVICES = [
    (320, 568, 2),   # SE (1st gen)
    (375, 667, 2),   # 6/7/8, SE 2/3
    (414, 736, 3),   # 6/7/8 Plus
    (375, 812, 3),   # X/XS/11 Pro, 12/13 mini
    (414, 896, 2),   # XR/11
    (414, 896, 3),   # XS Max/11 Pro Max
    (390, 844, 3),   # 12/13/14, 12/13 Pro, 16e
    (428, 926, 3),   # 12/13 Pro Max, 14 Plus
    (393, 852, 3),   # 14 Pro, 15, 15 Pro, 16
    (430, 932, 3),   # 14 Pro Max, 15 Plus/Pro Max, 16 Plus
    (402, 874, 3),   # 16 Pro, 17, 17 Pro
    (420, 912, 3),   # Air
    (440, 956, 3),   # 16 Pro Max, 17 Pro Max
]
THEMES = {"light": "#f5f2ed", "dark": "#151412"}   # --bg in app.css


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    icon = "file:///" + os.path.abspath(os.path.join(STATIC, "icon.svg")).replace("\\", "/")
    links = []
    with tempfile.TemporaryDirectory() as tmp:
        for w, h, r in DEVICES:
            pw, ph = w * r, h * r
            for theme, bg in THEMES.items():
                size = round(min(pw, ph) * 0.28)
                html = os.path.join(tmp, "s.html")
                with open(html, "w") as f:
                    f.write(f'<html><body style="margin:0;background:{bg};width:{pw}px;height:{ph}px;'
                            f'display:flex;align-items:center;justify-content:center">'
                            f'<img src="{icon}" width="{size}" height="{size}"></body></html>')
                name = f"{pw}x{ph}-{theme}.png"
                subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                                f"--window-size={pw},{ph}", f"--screenshot={os.path.join(OUT, name)}",
                                "file:///" + html.replace("\\", "/")],
                               check=True, capture_output=True)
                links.append(f'<link rel="apple-touch-startup-image" href="splash/{name}" media="'
                             f'(device-width: {w}px) and (device-height: {h}px) and '
                             f'(-webkit-device-pixel-ratio: {r}) and (orientation: portrait) and '
                             f'(prefers-color-scheme: {theme})">')
    print("\n".join(links))
    return 0


if __name__ == "__main__":
    sys.exit(main())
