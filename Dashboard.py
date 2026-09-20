#!/usr/bin/env python3
"""\file Dashboard.py
\brief Process entry point for the Quality Gate Dashboard.

The implementation lives under ``src``. This file only prepares imports and
delegates startup to the composition root.
"""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.backend.bootstrap import main


if __name__ == "__main__":
    main()#!/usr/bin/env python3