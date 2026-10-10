"""ArcBench runs `python3 main.py` at the package root; the agent itself lives in src/."""

import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))
runpy.run_path(str(SRC / "main.py"), run_name="__main__")
