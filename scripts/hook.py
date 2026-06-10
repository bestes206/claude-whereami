#!/usr/bin/env python3
"""Plugin shim: run the whereami Stop hook straight from the plugin directory.

whereami is stdlib-only, so no venv or install step is needed — pointing
sys.path at the bundled src/ is the entire bootstrap.
"""
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"),
)

from whereami.drift import run_hook

if __name__ == "__main__":
    run_hook()
