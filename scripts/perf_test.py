"""Run reproducible, isolated performance tests; see docs/performance-testing.md."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.perf_toolkit.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
