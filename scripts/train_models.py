#!/usr/bin/env python3
"""Train PatchTST model for volky-bot (delegates to main project training script).

Usage:
    python scripts/train_models.py              # PatchTST 15m (default)
    python scripts/train_models.py --all        # All available timeframes
    python scripts/train_models.py --timeframe 5m
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MAIN_PROJECT = Path.home() / "Desktop/2026"
TRAIN_SCRIPT = MAIN_PROJECT / "scripts" / "train_patchtst_15m.py"
VENV_PYTHON = MAIN_PROJECT / "venv-chronos311" / "bin" / "python"

if not VENV_PYTHON.exists():
    VENV_PYTHON = MAIN_PROJECT / "venv-mac" / "bin" / "python"


def main():
    parser = argparse.ArgumentParser(description="Train PatchTST for volky-bot")
    parser.add_argument("--timeframe", "-t", default="15m", choices=["1m", "5m", "15m", "1h"])
    parser.add_argument("--all", action="store_true", help="Train all timeframes")
    parser.add_argument("--epochs", type=int, default=60)
    args = parser.parse_args()

    if not TRAIN_SCRIPT.exists():
        print(f"[ERROR] Training script not found: {TRAIN_SCRIPT}")
        sys.exit(1)

    if not VENV_PYTHON.exists():
        print(f"[ERROR] Python venv not found: {VENV_PYTHON}")
        sys.exit(1)

    cmd = [str(VENV_PYTHON), str(TRAIN_SCRIPT)]
    if args.all:
        cmd.append("--all-timeframes")
    else:
        cmd.extend(["--timeframe", args.timeframe])
    cmd.extend(["--epochs", str(args.epochs)])

    print(f"Training PatchTST: {' '.join(cmd)}")
    print(f"Output: {MAIN_PROJECT / 'models' / 'patchtst'}")
    print()

    result = subprocess.run(cmd, cwd=str(MAIN_PROJECT))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
