"""Package the browser extension for Firefox (.xpi = zip with the Firefox manifest).

    python tools/build_firefox.py [output.xpi]
"""

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "browser-extension")


def build(out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, _, files in os.walk(SRC):
            for name in sorted(files):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, SRC).replace(os.sep, "/")
                if rel in ("manifest.json", "manifest.firefox.json"):
                    continue
                z.write(path, rel)
        z.write(os.path.join(SRC, "manifest.firefox.json"), "manifest.json")
    return out


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "SwiftGet-firefox.xpi")
    print(build(target))
