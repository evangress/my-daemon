# SPDX-License-Identifier: Apache-2.0
"""Tkinter setup window — vault picker + API key entry.

Saves the vault path to ``config.yaml`` and persists ``ANTHROPIC_API_KEY``:
- updates ``.env`` (always — read by load_settings())
- on Windows, also runs ``setx`` so the key is set as a true OS env var
  for new processes
- updates ``os.environ`` for the current process so anything launched
  next (e.g. ``daemon chat``) picks it up immediately

Tkinter chosen for zero extra runtime deps — the heavier NiceGUI surface
is reserved for the daemon's chat itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import yaml
from dotenv import set_key

from my_daemon.paths import CONFIG_FILENAME, find_config, log_path

# Brand palette — hex approximations of the OKLCH values from THEME.md.
# Tkinter doesn't support oklch(); these are the closest sRGB equivalents.
BG = "#16162A"          # deep indigo background
SURFACE = "#1F1F32"     # raised panel
INK = "#EEEAE0"         # warm near-white
INK_MUTE = "#A8A39A"    # softened body text
VIOLET = "#A86CFF"      # daemon — primary CTA
CYAN = "#5BCFD8"        # synthesis
GOLD = "#E2B27E"        # human spark — focus / save success
RULE = "#3A3A55"        # hairline borders
ERROR = "#E37070"

ENV_KEY = "ANTHROPIC_API_KEY"

# Windows Task Scheduler job name for the daily reflection run.
REFLECT_TASK_NAME = "MyDaemonReflect"
REFLECT_TASK_TIME = "03:00"


def config_path() -> Path:
    """The config this window edits.

    Resolved through the same search order the daemon itself uses, so the
    window edits the file the daemon will read. Only when nothing exists
    anywhere does it fall back to the current directory — i.e. the file
    `daemon init` would have written.

    Deliberately a function, not an import-time constant: the old
    `PROJECT_ROOT = Path.cwd()` meant `daemon setup` wrote config.yaml into
    whatever directory the launcher happened to start in.
    """
    found = find_config()
    return found if found is not None else Path.cwd() / CONFIG_FILENAME


def project_root() -> Path:
    """Directory the config lives in — the anchor for its relative paths."""
    return config_path().parent


def _config_example() -> Path:
    return project_root() / "config.example.yaml"


def _dotenv_path() -> Path:
    """`.env` sits beside the config, which is where `load_settings` reads it."""
    return project_root() / ".env"


def _load_yaml() -> dict:
    """Read config.yaml; fall back to config.example.yaml; finally an empty dict."""
    for path in (config_path(), _config_example()):
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
    return {}


def _save_vault_path(vault_path: str) -> None:
    """Persist vault.path to config.yaml, preserving other settings."""
    data = _load_yaml()
    data.setdefault("vault", {})["path"] = vault_path
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)


def _persist_api_key(api_key: str) -> tuple[bool, str]:
    """Persist the key. Returns (success, status message).

    Writes to .env in all cases; on Windows, additionally runs `setx` so the
    key is a true persistent user-level OS env var. Also updates os.environ
    so anything launched from this process sees it without a restart.
    """
    # .env — handles quoting/escaping correctly.
    dotenv_path = _dotenv_path()
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    dotenv_path.touch(exist_ok=True)
    set_key(str(dotenv_path), ENV_KEY, api_key, quote_mode="never")

    os.environ[ENV_KEY] = api_key

    if sys.platform == "win32":
        # setx writes to HKCU\Environment; new processes inherit it. Hide the
        # console flash with CREATE_NO_WINDOW (0x08000000).
        try:
            subprocess.run(
                ["setx", ENV_KEY, api_key],
                check=True,
                capture_output=True,
                text=True,
                creationflags=0x08000000,
            )
            return True, f"Saved. {ENV_KEY} is set as a Windows user env var (new shells will see it)."
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            return False, f"Wrote .env, but `setx` failed: {exc}"

    return True, (
        f"Saved to .env. To make {ENV_KEY} a permanent OS env var on this system, "
        f"add `export {ENV_KEY}=...` to ~/.profile or ~/.zshrc."
    )


def _daemon_exe_path() -> Path:
    """Resolve the bundled daemon entry point inside the project's venv."""
    return project_root() / ".venv" / "Scripts" / "daemon.exe"


def _reflect_task_exists() -> bool:
    if sys.platform != "win32":
        return False
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", REFLECT_TASK_NAME],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    return res.returncode == 0


def reflect_task_command(exe: Path, cfg: Path) -> str:
    """The `/TR` string for the scheduled reflect job.

    A Windows scheduled task starts in `system32`, and `schtasks` has no
    working-directory switch outside XML task definitions. So the working
    directory is made irrelevant instead: `--config` names the file
    absolutely, and every relative state path in it anchors to *its* directory,
    not the process CWD. That also puts `.env` (the API key) back in scope.
    """
    return f'"{exe}" --config "{cfg}" reflect'


def _schedule_reflect_task() -> tuple[bool, str]:
    """Register / refresh the daily 03:00 reflect job in Windows Task Scheduler."""
    if sys.platform != "win32":
        return False, "Scheduling is Windows-only; use cron on Linux/macOS."
    exe = _daemon_exe_path()
    if not exe.is_file():
        return False, f"{exe} not found — run setup.bat first."
    try:
        subprocess.run(
            [
                "schtasks", "/Create",
                "/SC", "DAILY",
                "/TN", REFLECT_TASK_NAME,
                "/TR", reflect_task_command(exe, config_path()),
                "/ST", REFLECT_TASK_TIME,
                "/F",
            ],
            check=True, capture_output=True, text=True, creationflags=0x08000000,
        )
        return True, f"Scheduled `daemon reflect` daily at {REFLECT_TASK_TIME} (task: {REFLECT_TASK_NAME})."
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return False, f"schtasks /Create failed: {exc}"


def _unschedule_reflect_task() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Scheduling is Windows-only."
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", REFLECT_TASK_NAME, "/F"],
            check=True, capture_output=True, text=True, creationflags=0x08000000,
        )
        return True, f"Removed scheduled task {REFLECT_TASK_NAME}."
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return False, f"schtasks /Delete failed: {exc}"


def _style_widgets(root: tk.Tk) -> ttk.Style:
    """Apply the brand palette to ttk widgets via the 'clam' theme (cross-platform)."""
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=INK, fieldbackground=SURFACE,
                    bordercolor=RULE, lightcolor=BG, darkcolor=BG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=INK, font=("Georgia", 11))
    style.configure("Eyebrow.TLabel", background=BG, foreground=INK_MUTE,
                    font=("Courier", 9))
    style.configure("Heading.TLabel", background=BG, foreground=INK,
                    font=("Georgia", 18, "italic"))
    style.configure("Status.TLabel", background=BG, foreground=INK_MUTE,
                    font=("Georgia", 10, "italic"))
    style.configure("Error.TLabel", background=BG, foreground=ERROR,
                    font=("Georgia", 10, "italic"))
    style.configure("Success.TLabel", background=BG, foreground=GOLD,
                    font=("Georgia", 10, "italic"))

    style.configure("TEntry", fieldbackground=SURFACE, foreground=INK,
                    bordercolor=RULE, insertcolor=GOLD, padding=6,
                    relief="flat")
    style.map("TEntry",
              bordercolor=[("focus", GOLD)],
              fieldbackground=[("focus", SURFACE)])

    style.configure("TButton", background=SURFACE, foreground=INK,
                    bordercolor=RULE, padding=(14, 8), relief="flat",
                    font=("Georgia", 10))
    style.map("TButton",
              background=[("active", "#2A2A44"), ("pressed", "#2A2A44")],
              foreground=[("active", INK), ("pressed", INK)])

    style.configure("Primary.TButton", background=VIOLET, foreground="#15102A",
                    padding=(20, 9), font=("Courier", 9, "bold"))
    style.map("Primary.TButton",
              background=[("active", "#B985FF"), ("pressed", "#9358EE")])

    style.configure("Link.TCheckbutton", background=BG, foreground=INK_MUTE,
                    font=("Courier", 9))
    style.map("Link.TCheckbutton", background=[("active", BG)])

    return style


class SetupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("My Daemon — Setup")
        self.root.configure(background=BG)
        self.root.geometry("620x360")
        self.root.minsize(560, 320)

        _style_widgets(self.root)

        # Prefill from current state.
        existing = _load_yaml()
        existing_vault = (existing.get("vault") or {}).get("path", "")
        existing_key = os.environ.get(ENV_KEY, "")

        self.vault_var = tk.StringVar(value=str(Path(existing_vault).expanduser()) if existing_vault else "")
        self.key_var = tk.StringVar(value=existing_key)
        self.show_key_var = tk.BooleanVar(value=False)
        # Scheduling — preselect if the task already exists so Save is non-destructive.
        self.schedule_var = tk.BooleanVar(value=_reflect_task_exists())
        self.status_var = tk.StringVar(value="")
        self.status_style = tk.StringVar(value="Status.TLabel")

        self._build()

    def _build(self) -> None:
        container = ttk.Frame(self.root, padding=(28, 24, 28, 20))
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        # Header — wordmark-ish heading + eyebrow.
        ttk.Label(container, text="✦  My Daemon", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(container, text="SETUP · VAULT & KEY", style="Eyebrow.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 18)
        )

        # Vault path row.
        ttk.Label(container, text="Obsidian vault folder").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        vault_entry = ttk.Entry(container, textvariable=self.vault_var, width=50)
        vault_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(container, text="Browse…", command=self._pick_vault).grid(
            row=3, column=2, sticky="e"
        )

        # API key row.
        ttk.Label(container, text="Anthropic API key").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(18, 4)
        )
        self.key_entry = ttk.Entry(container, textvariable=self.key_var, show="•", width=50)
        self.key_entry.grid(row=5, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Checkbutton(
            container, text="show", variable=self.show_key_var,
            command=self._toggle_show_key, style="Link.TCheckbutton",
        ).grid(row=5, column=2, sticky="e")

        ttk.Label(
            container,
            text="Stored in .env (gitignored). On Windows also set as a user env var via `setx`.",
            style="Status.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Scheduling — Windows-only; hide the row entirely on other OSes.
        if sys.platform == "win32":
            ttk.Checkbutton(
                container,
                text=f"Run `daemon reflect` daily at {REFLECT_TASK_TIME} (Windows Task Scheduler)",
                variable=self.schedule_var,
                style="Link.TCheckbutton",
            ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(14, 0))

        # Where the chat window logs go. With --native the console is hidden,
        # so this is the only place crashes show up.
        ttk.Label(
            container,
            text=f"Chat log: {log_path()}",
            style="Eyebrow.TLabel",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(14, 0))

        # Footer — Save button right-aligned, status to its left.
        footer = ttk.Frame(container)
        footer.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        footer.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")

        ttk.Button(footer, text="CANCEL", command=self.root.destroy).grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )
        ttk.Button(footer, text="SAVE", command=self._on_save, style="Primary.TButton").grid(
            row=0, column=2, sticky="e"
        )

        # Enter from anywhere → Save.
        self.root.bind("<Return>", lambda _e: self._on_save())

    def _pick_vault(self) -> None:
        initial = self.vault_var.get() or str(Path.home())
        chosen = filedialog.askdirectory(
            initialdir=initial,
            title="Select your Obsidian vault folder",
            mustexist=True,
            parent=self.root,
        )
        if chosen:
            self.vault_var.set(chosen)

    def _toggle_show_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _set_status(self, text: str, style: str = "Status.TLabel") -> None:
        self.status_var.set(text)
        self.status_label.configure(style=style)

    def _on_save(self) -> None:
        vault = self.vault_var.get().strip()
        key = self.key_var.get().strip()

        if not vault:
            self._set_status("Pick a vault folder before saving.", "Error.TLabel")
            return
        if not Path(vault).expanduser().is_dir():
            self._set_status(f"Vault folder not found: {vault}", "Error.TLabel")
            return
        if not key:
            self._set_status("API key is empty — paste it before saving.", "Error.TLabel")
            return

        try:
            _save_vault_path(str(Path(vault).expanduser()))
        except OSError as exc:
            self._set_status(f"Couldn't write config.yaml: {exc}", "Error.TLabel")
            return

        ok, msg = _persist_api_key(key)

        # Scheduling — only meaningful on Windows.
        sched_msg = ""
        if sys.platform == "win32":
            want = bool(self.schedule_var.get())
            have = _reflect_task_exists()
            if want and not have:
                s_ok, s_msg = _schedule_reflect_task()
                sched_msg = " " + s_msg
                ok = ok and s_ok
            elif have and not want:
                s_ok, s_msg = _unschedule_reflect_task()
                sched_msg = " " + s_msg
                ok = ok and s_ok

        self._set_status(msg + sched_msg, "Success.TLabel" if ok else "Error.TLabel")


def launch_setup() -> None:
    """Open the setup window. Blocks until the user closes it."""
    SetupWindow().root.mainloop()


if __name__ == "__main__":
    launch_setup()
