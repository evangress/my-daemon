# `gui/` — Chat window and setup window

Source: `src/my_daemon/gui/`.

Two distinct surfaces with very different jobs and very different stacks.

## `gui.app` — NiceGUI chat (`daemon chat`)

The warm-themed chat. Brand-aligned: deep midnight indigo with three
corner-anchored radial gradients (violet, cyan, indigo) to read as
paper-warmth in the dark rather than terminal black. The trichromatic story
— a question (gold) calls a daemon (violet) which speaks back (cyan) — is
the visual spine. See [Theme](../theme.md) for the full palette and
component reference.

### Entry point

```python
launch_chat(host="127.0.0.1", port=8765, native=True)
```

Builds the long-lived `_DaemonContext` once, then calls `ui.run(root=...)`
with a closure that mounts the UI per client. This `root=callable` shape is
load-bearing — NiceGUI 3.x rejects "script mode requires…" if you try to
build elements at module import time when launching from a non-script
entry point (which is exactly the CLI's situation).

`native` defaults to `True` — the chat opens as a real desktop window via
pywebview (`pywebview>=5.0` is now a hard dependency, not an optional
extra). Pass `native=False` to expose the UI as a browser tab; the CLI
flag is `daemon chat --no-native`.

### `_DaemonContext`

```python
@dataclass
class _DaemonContext:
    settings: Settings
    embedder: Embedder
    sparse_embedder: SparseEmbedder | None
    vector_store: VectorStore
    graph_store: GraphStore        # loaded eagerly at startup
    feedback_store: FeedbackStore
    llm: LLMClient
    orchestrator: RetrievalOrchestrator
```

Built once at startup so subsequent sends don't re-load the embedding model
or re-parse the graph pickle. The embedding model itself still loads
lazily on first encode, so the first send takes a few seconds and the
rest are warm.

### Streaming

`_stream_into_label()` drains a sync `Iterator[str]` (from
`LLMClient.synthesize_stream`) into the NiceGUI markdown label via an
`asyncio.Queue` + a producer thread. This keeps the event loop responsive
while the SDK's streaming API yields bytes; errors get surfaced into the
bubble instead of crashing the UI.

### Send handler

`handle_send()`:

1. Disables the input + Send button while busy (no double-fire).
2. Renders the user bubble (gold-stripe, italic Fraunces) and an empty
   daemon bubble (cyan-stripe, Newsreader body, markdown-parsed inline).
3. Calls `orchestrator.retrieve()` off the event loop via `asyncio.to_thread`.
4. If nothing ranked, renders a "_Nothing in your notes matched._" message.
5. Otherwise streams the synthesis into the daemon bubble.
6. Logs a `FeedbackEvent` with latency, query, answer, and a compact
   retrieval summary.
7. Re-enables the controls and re-focuses the input.

### Lazy graph load

`graph_store.load()` is called once during context construction. If the
manifest exists but the gpickle doesn't, the graph is just empty — the
expander quietly returns no neighbors and the chat falls back to dense
retrieval only.

### File logging

Because the native window hides stdout, `launch_chat` attaches a
`RotatingFileHandler` to the root logger at startup. The path comes from
`my_daemon.paths.log_path()` and is platform-aware (`%LOCALAPPDATA%` on
Windows, `~/Library/Logs/` on macOS, `$XDG_STATE_HOME` on Linux).
Unhandled exceptions inside the send handler are logged with full
traceback to that file, in addition to surfacing into the daemon bubble
as an error message.

### Launchers

`setup.py` writes platform-specific launcher shims at project bootstrap:

- **Windows:** `launch-gui.vbs` (silent — `WScript.Run` with `WindowStyle=0`)
  is the default. `launch-gui.bat` is a debug fallback that forces
  `--no-native` and keeps a visible console.
- **Linux:** `launch-gui.sh` + `my-daemon.desktop`. Copy the `.desktop`
  file into `~/.local/share/applications/` to expose the daemon in the
  activities menu.
- **macOS:** `launch-gui.command` (Finder-double-clickable shim around
  `launch-gui.sh`).

## `gui.setup` — Tkinter setup window (`daemon setup`)

Why Tkinter instead of NiceGUI? Setup runs *before* the daemon has any data
— before Qdrant is up, before the model is downloaded, before the API key
is even set. NiceGUI's startup time would be obnoxious here, and Tkinter
ships with Python so there's nothing to install.

### What it does

1. Read `config.yaml` (falling back to `config.example.yaml`) and prefill
   the vault and API-key fields.
2. Vault picker (`filedialog.askdirectory`) — saves to `vault.path` in
   `config.yaml`, preserving every other setting via a round-trip through
   `yaml.safe_load` / `yaml.safe_dump`.
3. API key entry — writes to `.env` via `python-dotenv.set_key`, also
   updates `os.environ` for the current process. On Windows, additionally
   runs `setx ANTHROPIC_API_KEY ...` so new shells inherit the key as a
   user-level OS env var.
4. **Windows-only:** checkbox to register `MyDaemonReflect` in Task
   Scheduler via `schtasks /Create` (daily at 03:00, runs
   `.venv\Scripts\daemon.exe reflect`). Unchecking it on a subsequent save
   removes the task with `schtasks /Delete`.

### Styling

A hex approximation of the OKLCH brand palette, applied via `ttk.Style` and
the `clam` theme (Tkinter doesn't speak OKLCH). The visual story matches
the chat: violet primary CTA, gold focus / success accent, deep indigo
background.

### Why both at once

Setup is a one-shot configuration tool. Chat is a long-lived runtime
companion. Trying to share the same UI stack would force one of them into
ergonomics it doesn't want.
