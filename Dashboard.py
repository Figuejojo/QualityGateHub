#!/usr/bin/env python3
"""Application entry point; implementation lives under ``src``."""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.backend.bootstrap import main


if __name__ == "__main__":
    main()#!/usr/bin/env python3