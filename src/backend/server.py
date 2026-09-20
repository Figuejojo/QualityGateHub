"""Compatibility module for the refactored dashboard backend.

The implementation is organized under domain, application, infrastructure,
and interfaces. New code should import the composition root from bootstrap.
"""

from .bootstrap import Application, main

__all__ = ["Application", "main"]


if __name__ == "__main__":
    main()
