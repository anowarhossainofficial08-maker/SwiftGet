"""Entry point used for `python run.py` and for building the .exe with PyInstaller."""

import sys

from swiftget.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
