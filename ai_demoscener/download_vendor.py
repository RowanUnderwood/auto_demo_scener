"""Download three.js files into static/three/. Run once before starting the server."""

import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
THREE_DIR = BASE / "static" / "three"
THREE_DIR.mkdir(parents=True, exist_ok=True)

VERSION = "0.160.0"

FILES = {
    "three.module.js": f"https://unpkg.com/three@{VERSION}/build/three.module.js",
    "three.min.js":    f"https://unpkg.com/three@{VERSION}/build/three.min.js",
}


def download(name: str, url: str) -> None:
    dest = THREE_DIR / name
    if dest.exists():
        print(f"  {name}: already exists ({dest.stat().st_size:,} bytes)")
        return
    print(f"  {name}: downloading from {url} …", end=" ", flush=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        data = resp.read()
        f.write(data)
    print(f"done ({len(data):,} bytes)")


if __name__ == "__main__":
    print(f"Downloading three.js v{VERSION} to {THREE_DIR}")
    for name, url in FILES.items():
        download(name, url)
    print("Done.")
