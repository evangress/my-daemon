"""My Daemon — cross-platform bootstrap helper.

Run me from the project root to:
  1. Verify Python version
  2. Create a .venv (uses uv if available, else stdlib venv + pip)
  3. Install the project in editable mode with dev extras
  4. Copy config.example.yaml -> config.yaml and .env.example -> .env (if missing)
  5. Print next steps with the activate command for your OS

Usage:
  Windows:   double-click setup.bat   (or:  python setup.py)
  Linux/Mac: python setup.py

NOTE: This is *not* a setuptools build script. The project's actual build
system is hatchling, configured in pyproject.toml. This file is a
project-level convenience bootstrap and is ignored by pip during builds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

MIN_PY = (3, 11)
ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"

IS_WINDOWS = os.name == "nt"


def section(msg: str) -> None:
    print(f"\n=== {msg} ===")


def info(msg: str) -> None:
    print(f"  - {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def fail(msg: str, code: int = 1) -> NoReturn:
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)


def check_python_version() -> None:
    section(f"Python version (need >= {MIN_PY[0]}.{MIN_PY[1]})")
    if sys.version_info < MIN_PY:
        fail(
            f"Found Python {sys.version_info.major}.{sys.version_info.minor}. "
            f"Install Python {MIN_PY[0]}.{MIN_PY[1]}+ from https://www.python.org/downloads/."
        )
    info(f"OK — Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def venv_python() -> Path:
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def have_uv() -> bool:
    return shutil.which("uv") is not None


def create_venv() -> None:
    section("Virtual environment (.venv)")
    if VENV_DIR.exists():
        info(f"Reusing existing .venv at {VENV_DIR}")
        return

    if have_uv():
        info("Found uv on PATH — using `uv venv`")
        subprocess.run(["uv", "venv", str(VENV_DIR)], check=True, cwd=ROOT)
    else:
        info("uv not found — using stdlib `python -m venv`")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True, cwd=ROOT)
    info(f"Created {VENV_DIR}")


def install_deps() -> None:
    section("Install project dependencies (editable + [dev])")
    target = ".[dev]"
    if have_uv():
        info("Using `uv pip install` (faster)")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv_python()), "-e", target],
            check=True,
            cwd=ROOT,
        )
    else:
        info("Using the venv's pip")
        py = str(venv_python())
        # Ensure pip is current; new venvs sometimes ship with an old pip
        subprocess.run([py, "-m", "ensurepip", "--upgrade"], check=False, cwd=ROOT)
        subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], check=True, cwd=ROOT)
        subprocess.run([py, "-m", "pip", "install", "-e", target], check=True, cwd=ROOT)
    info("Dependencies installed")


def copy_template(src_name: str, dest_name: str) -> None:
    src = ROOT / src_name
    dest = ROOT / dest_name
    if not src.is_file():
        warn(f"Missing template {src_name} — skipping")
        return
    if dest.exists():
        info(f"{dest_name} already exists — leaving it alone")
        return
    shutil.copy(src, dest)
    info(f"Created {dest_name} from {src_name}")


def seed_configs() -> None:
    section("Configuration files")
    copy_template("config.example.yaml", "config.yaml")
    copy_template(".env.example", ".env")


def print_next_steps() -> None:
    section("Next steps")
    activate = r".venv\Scripts\activate.bat" if IS_WINDOWS else "source .venv/bin/activate"

    print(
        f"""
  1. Open .env and set ANTHROPIC_API_KEY to your real key.
  2. Open config.yaml and point `vault.path` at your Obsidian vault.
  3. Start Qdrant (one-time per machine):

       docker compose up -d

  4. Activate the environment in your shell:

       {activate}

  5. Ingest your vault and ask your first question:

       daemon ingest -v
       daemon query "what was I thinking about last week"

  Full reference: README.md
"""
    )


def main() -> int:
    print("My Daemon — bootstrap")
    print(f"Project root: {ROOT}")
    try:
        check_python_version()
        create_venv()
        install_deps()
        seed_configs()
        print_next_steps()
    except subprocess.CalledProcessError as exc:
        fail(f"Command failed: {exc.cmd} (exit {exc.returncode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
