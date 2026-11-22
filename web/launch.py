"""Launch the TradingAgents web UI via `tradingagents-web` command."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    app_path = Path(__file__).parent / "app.py"
    os.environ.setdefault("STREAMLIT_SERVER_ADDRESS", "127.0.0.1")
    command = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    if os.name == "nt":
        raise SystemExit(subprocess.call(command))
    # In Docker, let Streamlit receive the container stop signal directly.
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
