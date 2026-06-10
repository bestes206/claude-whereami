#!/usr/bin/env python3
"""Plugin/clone shim: run `whereami install` straight from this directory.

whereami is stdlib-only, so no venv or install step is needed — pointing
sys.path at the bundled src/ is the entire bootstrap. Passing this script's
own directory lets the installer wire the statusline/hook against the sibling
shims (statusline.py, hook.py) that live right here.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "src"))

from whereami.install import main

if __name__ == "__main__":
    raise SystemExit(main(scripts_dir=_HERE))
