import sys

from .cli import main

if __name__ == "__main__":   # required on Windows/macOS (spawned helper processes re-import main)
    sys.exit(main())
