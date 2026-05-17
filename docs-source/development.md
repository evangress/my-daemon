# Development

## Setup

```bash
git clone https://github.com/evangress/my-daemon
cd my-daemon
python setup.py                   # creates .venv, installs deps + dev extras
# or, manually:
uv sync                            # or: pip install -e ".[dev]"
```

The dev extras add `pytest`, `pytest-asyncio`, `ruff`, and `mypy`.

## Project layout

```
my-daemon/
├── src/my_daemon/         # the package
│   ├── cli.py             # Typer entry — `daemon` script
│   ├── config.py          # Pydantic Settings (yaml + env)
│   ├── models.py          # Note / Chunk / RetrievedChunk / FeedbackEvent
│   ├── vault/             # markdown → Notes + Chunks; safe writeback
│   ├── embeddings/        # dense + sparse encoders
│   ├── stores/            # Qdrant, NetworkX, SQLite
│   ├── retrieval/         # seed → expand → orchestrate
│   ├── llm/               # Anthropic client, prompts, agent prompts
│   ├── pipeline/          # ingest, query, agent_extract/link/reflect
│   └── gui/               # NiceGUI chat (app.py), Tkinter setup (setup.py)
├── tests/                 # pytest; fixtures/sample_vault for the parser tests
├── scripts/               # inspect_state.py, reset.py, license_check.py (dev convenience)
├── docs-source/           # this site's markdown source
├── docs-site/             # built HTML output (gitignored)
├── debug/error-log.md     # session error log
├── config.example.yaml    # template config
├── docker-compose.yaml    # Qdrant
└── pyproject.toml
```

## Running tests

```bash
pytest                                  # all unit tests; no external services
MY_DAEMON_E2E=1 pytest tests/test_e2e.py  # full pipeline; needs Qdrant up
```

The non-e2e tests use the fixture vault under `tests/fixtures/sample_vault/`
and assert on parser / chunker / graph / writer behaviour without touching
Qdrant or Claude. The e2e test ingests the fixture and asserts that a
query returns expected citations — it requires `docker compose up -d`
beforehand.

## Linting

```bash
ruff check .
```

The ruff config (in `pyproject.toml`) selects `E`, `F`, `I`, `B`, `UP`,
`SIM`, ignores `E501`, and targets py311. CI is not yet wired up; the
project-level rule from `CLAUDE.md` is "lint before commit."

## Building these docs

```bash
.venv/bin/mkdocs build         # writes ./docs-site/
.venv/bin/mkdocs serve         # local preview at http://127.0.0.1:8000
```

Only stock MkDocs is installed (the project deliberately avoids
mkdocs-material to keep dev dependencies tight). The theme is the built-in
`readthedocs` theme; nav is declared explicitly in `mkdocs.yaml`.

## Session hygiene

From `CLAUDE.md`:

1. Any file with secrets goes in `.gitignore`.
2. Check `PROJECT_MANAGEMENT.md` for pending tasks at the start of a session.
3. Check `debug/error-log.md` for errors to resolve.
4. At end of session: lint, `git add .`, commit to `master` (dev branches
   land post-release).
5. Speculative ideas go in `PROJECT_MANAGEMENT.md`'s "AI Suggestions"
   section, not into code.

## Adding a CLI command

1. Add the function under `src/my_daemon/cli.py` with a `@app.command()`
   decorator (or `@graph_app.command()` etc. for a subgroup).
2. Build the dependencies with the existing `_build_*` helpers — they keep
   construction consistent and pick up the hybrid / cache_folder flags
   correctly.
3. Use `Console` (rich) for output. Tables for structured results; `Panel`
   for highlighted blocks; `console.log(...)` for verbose-mode debug.
4. Add a docstring as the help text — Typer surfaces it on `--help`.

## Adding a config key

1. Add it to the relevant `BaseModel` in `src/my_daemon/config.py` with a
   sensible default.
2. Mirror it in `config.example.yaml` (and in `config.yaml` for your local
   dev, ideally not committed).
3. Reference it from the consumer code via `settings.<section>.<key>`.
4. Document it under [Configuration](configuration.md).

Env-var override is automatic: any nested field is reachable as
`MY_DAEMON_<SECTION>__<KEY>`.

## Adding a writeback job

The shape to copy is `pipeline/agent_extract.py`. Required pieces:

- Eligibility filter (which notes / themes / scope this run touches).
- One or more LLM prompts in `llm/agents.py` returning structured payloads
  via `_coerce_json`.
- A render function that produces the markdown body.
- A call to the appropriate `vault/writer.py` function
  (`write_agent_section`, `insert_wikilinks`, `add_tags`, or
  `write_atomic` for full-file writes).
- A state table in `stores/agent_state.py` for dedup.
- A `@app.command` wrapper in `cli.py` that refuses to write unless
  `agent.enabled` is true (or `--dry-run`).

The safety discipline is the writer's responsibility, not yours. If you find
yourself adding gates inside your job, ask whether they belong in
`vault/writer.py` so the next job inherits them for free.

## Where to file ideas

`PROJECT_MANAGEMENT.md` has a top-level "AI Suggestions (Agent Thoughts)"
section. Half-formed ideas, future-phase work, and things deliberately not
built belong there — not in code comments or new files.
