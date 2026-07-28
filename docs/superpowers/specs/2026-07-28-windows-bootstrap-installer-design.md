# Windows bootstrap installer — design

- **Date:** 2026-07-28
- **Status:** approved, not yet implemented
- **Author:** Evan Gress, with Claude

## Problem

There is no documented way to *obtain* My Daemon. Both `README.md` and
`docs-source/getting-started.md` begin at "double-click `setup.bat`" and never
say where `setup.bat` comes from, whether to clone or download a ZIP, or where
the folder should live. A user hitting the docs cold has to guess.

The gap produced a real support case on 2026-07-28: the project was downloaded
as a ZIP and extracted into `%LOCALAPPDATA%`, which the docs implicitly
discouraged ("somewhere you own, e.g. `C:\Users\<you>\my-daemon`") without
explaining why. That instinct was in fact correct for a per-user Windows app;
the docs were wrong to steer away from it.

Two things follow: the project needs a real entry point, and the docs need a
step 0.

## Goals

1. A Windows 11 user can install My Daemon by downloading **one file** and
   double-clicking it, with no git, no ZIP wrangling, and no decision about
   where to put anything.
2. Installing does not require already understanding what a repository is.
3. The docs answer "how do I get this" before they answer "how do I run it".
4. The developer path — clone the repo, run `setup.py` — keeps working
   unchanged and is documented as such.

## Non-goals (deliberate)

- **Auto-update.** Re-running the installer updates in place. No background
  updater, no version-check nag.
- **Uninstaller.** Documented as "delete two folders". An app that writes only
  to its own directories does not need an uninstall program.
- **Code signing.** It costs real money and SmartScreen warns on first run
  regardless. The docs will explain the warning instead of hiding it.
- **MSI / MSIX packaging.** Far more machinery than this needs.
- **macOS / Linux bootstrappers.** Both platforms already have a working
  `python setup.py` path and users comfortable with it. Revisit if asked.

## Identity decision

My Daemon on Windows is **an app the user installed**, not a repository they
cloned. This drives every layout decision below, and was chosen over
"a project they cloned" and "both, detected at runtime" because the audience
named in `CLAUDE.md` — people managing cognitive decline, people with
developmental disabilities — must never need to know what git is.

## Architecture

### Components

**`install-my-daemon.bat`** (repo root, ~40 lines) — the only file a user
downloads. Its complete responsibility:

1. Locate Python: `py -3`, then `python`.
2. If absent, offer `winget install Python.Python.3.12` (winget ships with
   Windows 11). If the user declines or winget is missing, print the
   python.org URL and exit 1.
3. Download `scripts/bootstrap.py` over HTTPS to `%TEMP%`, from
   `https://raw.githubusercontent.com/evangress/my-daemon/master/scripts/bootstrap.py`.
4. Run it, passing through its exit code.

The bootstrapper is fetched from `master`, not from the release being
installed — a chicken-and-egg the design accepts deliberately. Resolving the
release requires the logic that lives in `bootstrap.py`, so something has to be
fetched unpinned first. Keeping that file small, reviewable, and in-repo is the
mitigation; it downloads the *pinned* release once it can.

Users obtain the `.bat` from
`https://github.com/evangress/my-daemon/raw/master/install-my-daemon.bat`,
which is the link the docs and README point at.

Kept this thin on purpose: batch is a poor language for JSON, ZIPs, and
shortcuts, and nothing in it can be tested by this repo's pytest harness. The
untestable surface is therefore reduced to "find Python, fetch one file".

**`scripts/bootstrap.py`** — the real installer, in a language this repo can
test.

| Function | Responsibility |
|---|---|
| `resolve_source()` | Determine what to install → `SourceRef(version, url, sha256 \| None)` |
| `download_and_verify(ref, dest)` | Fetch the ZIP; verify SHA-256 when one is known; abort before extraction on mismatch |
| `install_app(zip_path, root)` | Extract to `<root>\app\`, then delegate to the extracted `setup.py` |
| `init_user_config()` | Run `daemon init --user` only when no config exists |
| `create_shortcuts(root)` | Start Menu `.lnk` files via `WScript.Shell` |
| `launch_setup(root)` | Open the Setup window |
| `main()` | Sequence the above; render failures as one actionable line each |

**`tests/test_bootstrap.py`** — see [Testing](#testing).

### Resulting layout

```
%LOCALAPPDATA%\my-daemon\
└── app\                     extracted source, and .venv\ inside it
                             REPLACED WHOLESALE on update

%APPDATA%\my-daemon\
├── config.yaml              written once by `daemon init --user`
└── data\                    qdrant-local\, models\, graph.gpickle, feedback.db
                             NEVER touched by update

%APPDATA%\Microsoft\Windows\Start Menu\Programs\My Daemon\
├── My Daemon.lnk            → wscript app\launch-gui.vbs
└── My Daemon Setup.lnk      → app\.venv\Scripts\daemon.exe setup
```

Two properties matter here:

- **User state lives outside the app folder.** Update deletes and re-extracts
  `app\`; if config or the vector index lived there, updating would destroy
  the user's data. `daemon init --user` already implements exactly this split
  and is covered by `tests/test_config_resolution.py`.
- **The chat shortcut targets `launch-gui.vbs`, not `daemon.exe`.** A `.lnk`
  to `daemon.exe chat` pops a console window that stays open for the session,
  which reads as "a script ran", not "an app opened". `setup.py` already
  generates `launch-gui.vbs` precisely to avoid that; the Setup shortcut can
  target the exe directly because it exits immediately.
- **`.venv` lives *inside* `app\`.** `setup.py` already assumes
  `ROOT/.venv`, so this requires no change to it. The cost is that updating
  reinstalls dependencies (~1–2 min) instead of reusing the venv. Accepted:
  always correct, nothing clever, no partial-upgrade states.

### What gets installed

`resolve_source()` prefers, in order:

1. **A published release asset.** `GET /repos/evangress/my-daemon/releases/latest`;
   if it returns 200 and carries an asset named `my-daemon-<version>.zip`, use
   that asset's URL and its `digest` field for SHA-256 verification.
2. **The release source archive.** Same call, falling back to `zipball_url`.
   **No checksum is available** — GitHub publishes `digest` for uploaded
   assets but not for generated source archives. Verified 2026-07-28 against
   a repo that has releases.
3. **`master.zip`.** When the API returns 404 — which it does for this repo
   *today*, verified 2026-07-28, because no releases exist yet. This is the
   path that actually runs on day one.

Whichever branch is taken, the resolved version and URL are **printed before
the download**, and cases 2 and 3 additionally print that the archive could not
be checksum-verified. The installer never silently downgrades its own
integrity guarantees.

Cutting `v0.1.0` is a follow-up, not a blocker: the fallback works today, and
`PROJECT_MANAGEMENT.md` already tracks "version bump + tags" as open work.

## Security posture

A downloadable `.bat` that fetches and executes code over HTTPS is
structurally the `curl | bash` pattern. This is stated plainly rather than
papered over:

- All fetches are HTTPS to `github.com` / `api.github.com`. Certificate
  validation is Python's default and is never disabled.
- The resolved version and URL are printed before fetching, so the user can
  see what is about to run.
- SHA-256 is verified **when GitHub supplies a digest**, which today means
  only the release-asset path. The other two paths rely on TLS alone and say
  so.
- Anyone who can compromise the repository or a user's DNS gets code
  execution. That is inherent to this distribution model. The mitigation is a
  signed release asset with a published checksum, which the design leaves room
  for but does not yet require.

No claim is made that the result is "secure" — only that it is inspectable.

## Failure handling

Each of these is a path a real Windows user hits. Every one exits with a
single actionable line, never a traceback.

| Condition | Behavior |
|---|---|
| Python not found | Offer `winget install Python.Python.3.12`; on decline or missing winget, print python.org URL, exit 1 |
| Python outside 3.11–3.14 | Name the version found and the supported band, exit 1 |
| GitHub unreachable | "Could not reach github.com" + the URL tried, exit 1 |
| No release published | Fall back to `master.zip`, **print that it did**, continue |
| SHA-256 mismatch | Abort **before extracting**, exit 1 |
| Install already present | Prompt: update (replace `app\`, keep config and data) or cancel |
| Disk write denied | Name the path that failed, exit 1 |
| Tkinter unavailable | Skip auto-launch, print how to run Setup manually, exit 0 |
| Shortcut creation fails | Warn and continue — not fatal, the app still works |

## Documentation changes

**`docs-source/getting-started.md`** gains a new first section, before every
existing one:

- **Install (recommended).** Download `install-my-daemon.bat`, double-click.
  What SmartScreen will say and why. Where files land and why
  `%LOCALAPPDATA%` is the right place for a per-user Windows app.
- **Install from source.** The current flow, relabeled as the developer path,
  with the missing acquisition step written out — `git clone`, or download the
  ZIP and extract — instead of assumed.

**`README.md`** — the Windows quickstart collapses to the one-file download
and links onward. The from-source path stays for developers.

## Testing

`tests/test_bootstrap.py`, hand-written fakes per house style — no MagicMock,
no network:

- `resolve_source()` with a fake release payload carrying a matching asset →
  asset URL and digest chosen.
- Same, with a release but no matching asset → `zipball_url`, `sha256 is None`.
- API returning 404 → `master.zip`, and the fallback is announced.
- `download_and_verify()` with a matching digest → succeeds; with a
  mismatching digest → raises and **leaves no extracted files behind**.
- `install_app()` against a ZIP built in `tmp_path` → asserts the resulting
  tree, including that a pre-existing `config.yaml` outside `app\` survives.
- Existing-install detection.

OS-specific behavior — `create_shortcuts`, winget, and the `.bat` itself —
stays behind functions the tests do not call, so CI continues to pass on
Linux.

**Known verification gap, stated up front:** the `.bat`, winget invocation,
shortcut creation, and the Start Menu entries cannot be executed in the
development environment. They are written defensively and must be validated by
running the installer on a real Windows 11 machine. A round of fixes after
that first real run should be expected, not treated as a surprise.

## Open questions

None blocking. Deferred by choice: whether to cut `v0.1.0` before or after the
installer lands (the master fallback makes either order work), and whether to
publish a signed release asset with a checksums file to close the integrity
gap described under [Security posture](#security-posture).
