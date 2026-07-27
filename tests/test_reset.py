# SPDX-License-Identifier: Apache-2.0
"""`daemon reset` — the command with the most damage per keystroke.

What it used to do, verified against the code: hardcode `Path("./data")`,
ignore every configured path, `rmtree` the Qdrant storage directory out from
under a running container, and destroy the model cache, the snapshots and
`feedback.db` — the learned edge weights, the activation ledger and the themes,
none of which are re-derivable from the vault — behind a single prompt.
`scripts/reset.py` did the same with no prompt at all.

So the tests below are mostly about what reset *does not* touch. Three tiers,
by how expensive the loss is:

* free to lose (graph, manifest, vectors, reports) — deleted by default;
* a re-download (the model cache) — needs `--models`;
* irreplaceable (`feedback.db`) — needs `--all`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.config import Settings

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]

_BOX_CHARS = "┏┓┗┛━─│┃┡┩┠┨╇┼├┤┬┴╭╮╰╯╺╸"


def _squash(text: str) -> str:
    stripped = text.translate({ord(c): " " for c in _BOX_CHARS})
    return " ".join(stripped.split())


class FakeQdrantClient:
    def __init__(self) -> None:
        self.dropped: list[str] = []

    def delete_collection(self, collection_name: str) -> None:
        self.dropped.append(collection_name)


class FakeVectorStore:
    """Only the two things reset asks of a store: drop the collection, let go."""

    def __init__(self, collection: str = "chunks") -> None:
        self.collection = collection
        self.client = FakeQdrantClient()
        self.closed = False

    def _client_(self) -> FakeQdrantClient:
        return self.client

    def close(self) -> None:
        self.closed = True


def _touch(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _fill_dir(path: Path, names: tuple[str, ...] = ("a.bin",)) -> Path:
    for name in names:
        _touch(path / name, size=64)
    return path


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A fully populated, entirely disposable state tree, wired into `_load()`."""
    root = tmp_path / "home"
    s = Settings()
    s.config_path = root / "config.yaml"
    s.vault.path = tmp_path / "vault"
    s.vault.path.mkdir(parents=True)
    _touch(s.vault.path / "note.md")
    s.graph.path = _touch(root / "data" / "graph.gpickle")
    s.graph.manifest_path = _touch(root / "data" / "manifest.json")
    s.feedback.db_path = _touch(root / "data" / "feedback.db")
    _touch(root / "data" / "feedback.db-wal")
    _touch(root / "data" / "feedback.db-shm")
    s.embeddings.cache_folder = _fill_dir(root / "data" / "models")
    s.snapshot.dir = _fill_dir(root / "data" / "snapshots")
    s.consolidation.out_dir = _fill_dir(root / "data" / "consolidation")
    s.backup.dir = _fill_dir(root / "backups")
    s.vector_store.qdrant.url = "http://localhost:6333"
    s.vector_store.qdrant.path = _fill_dir(root / "data" / "qdrant-embedded")
    monkeypatch.setattr("my_daemon.cli._load", lambda: s)
    return s


@pytest.fixture
def server_state(state: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The same tree, but talking to a Qdrant server with a compose mount."""
    state.vector_store.qdrant.path = None
    state.vector_store.qdrant.url = "http://localhost:6333"
    _fill_dir(state.config_path.parent / "data" / "qdrant")
    return state


# ---------------------------------------------------------------------------
# config-awareness
# ---------------------------------------------------------------------------


def test_reset_deletes_the_configured_paths_not_cwd_data(
    state: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The old version wiped `./data` relative to wherever it was launched."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stray = _fill_dir(elsewhere / "data")
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert stray.is_dir(), "reset deleted a `./data` it was never pointed at"
    assert not state.graph.path.exists()
    assert not state.graph.manifest_path.exists()
    assert not state.snapshot.dir.exists()
    assert not state.consolidation.out_dir.exists()
    assert not state.vector_store.qdrant.path.exists()


def test_reset_lists_every_target_with_its_size_before_asking(
    state: Settings, monkeypatch: pytest.MonkeyPatch
):
    # A real terminal is wider than CliRunner's default 80 columns, and a path
    # folded across a column boundary cannot be matched (or pasted).
    monkeypatch.setenv("COLUMNS", "220")

    result = runner.invoke(app, ["reset"], input="no\n")

    assert result.exit_code == 1
    squashed = _squash(result.output)
    assert str(state.graph.path) in squashed
    assert str(state.snapshot.dir) in squashed
    # A size column, not just a list of names.
    assert "B" in squashed
    assert state.graph.path.exists(), "answering anything but yes must delete nothing"


def test_reset_aborts_without_confirmation(state: Settings):
    result = runner.invoke(app, ["reset"], input="\n")

    assert result.exit_code == 1
    assert state.graph.path.exists()


def test_reset_proceeds_on_yes_at_the_prompt(state: Settings):
    result = runner.invoke(app, ["reset"], input="yes\n")

    assert result.exit_code == 0, result.output
    assert not state.graph.path.exists()


# ---------------------------------------------------------------------------
# the three tiers
# ---------------------------------------------------------------------------


def test_the_model_cache_survives_by_default(state: Settings):
    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert state.embeddings.cache_folder.is_dir()
    assert "--models" in _squash(result.output)


def test_models_flag_removes_the_cache(state: Settings):
    result = runner.invoke(app, ["reset", "--yes", "--models"])

    assert result.exit_code == 0, result.output
    assert not state.embeddings.cache_folder.exists()


def test_feedback_db_survives_without_all(state: Settings):
    result = runner.invoke(app, ["reset", "--yes", "--models"])

    assert result.exit_code == 0, result.output
    assert state.feedback.db_path.is_file()
    assert "--all" in _squash(result.output)


def test_all_removes_the_feedback_db_and_its_sidecars(state: Settings):
    result = runner.invoke(app, ["reset", "--yes", "--all"])

    assert result.exit_code == 0, result.output
    assert not state.feedback.db_path.exists()
    assert not state.feedback.db_path.with_name("feedback.db-wal").exists()
    assert not state.feedback.db_path.with_name("feedback.db-shm").exists()


def test_backups_are_never_a_reset_target(state: Settings):
    result = runner.invoke(app, ["reset", "--yes", "--all", "--models"])

    assert result.exit_code == 0, result.output
    assert state.backup.dir.is_dir(), "reset ate the safety net"


def test_the_vault_is_never_touched(state: Settings):
    result = runner.invoke(app, ["reset", "--yes", "--all", "--models"])

    assert result.exit_code == 0, result.output
    assert (state.vault.path / "note.md").is_file()


# ---------------------------------------------------------------------------
# server mode
# ---------------------------------------------------------------------------


def test_a_live_servers_storage_is_refused_not_deleted(
    server_state: Settings, monkeypatch: pytest.MonkeyPatch
):
    """`rmtree` under a running container is how you corrupt a Qdrant volume."""
    storage = server_state.config_path.parent / "data" / "qdrant"
    monkeypatch.setattr("my_daemon.cli.server_reachable", lambda url, **kw: True)
    monkeypatch.setattr("my_daemon.cli._build_vector_store", lambda s, dim: FakeVectorStore())

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert storage.is_dir()
    assert "stop the container first" in _squash(result.output)


def test_a_live_server_has_its_collection_dropped_instead(
    server_state: Settings, monkeypatch: pytest.MonkeyPatch
):
    store = FakeVectorStore()
    monkeypatch.setattr("my_daemon.cli.server_reachable", lambda url, **kw: True)
    monkeypatch.setattr("my_daemon.cli._build_vector_store", lambda s, dim: store)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert store.client.dropped == ["chunks"]
    assert store.closed


def test_a_stopped_servers_storage_is_a_normal_target(
    server_state: Settings, monkeypatch: pytest.MonkeyPatch
):
    storage = server_state.config_path.parent / "data" / "qdrant"
    monkeypatch.setattr("my_daemon.cli.server_reachable", lambda url, **kw: False)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not storage.exists()


def test_an_unreachable_server_says_the_collection_was_not_dropped(
    server_state: Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("my_daemon.cli.server_reachable", lambda url, **kw: False)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "not dropped" in _squash(result.output)


def test_embedded_mode_never_opens_the_store_to_drop_a_collection(
    state: Settings, monkeypatch: pytest.MonkeyPatch
):
    """Deleting the folder *is* the drop; opening it would recreate it."""

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("embedded reset must not construct a VectorStore")

    monkeypatch.setattr("my_daemon.cli._build_vector_store", _explode)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not state.vector_store.qdrant.path.exists()


def test_nothing_to_remove_is_said_plainly(state: Settings, monkeypatch: pytest.MonkeyPatch):
    import shutil

    shutil.rmtree(state.config_path.parent / "data")
    monkeypatch.setattr("my_daemon.cli.server_reachable", lambda url, **kw: False)

    result = runner.invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Nothing to remove" in _squash(result.output)


# ---------------------------------------------------------------------------
# scripts/reset.py
# ---------------------------------------------------------------------------


def _load_script(name: str):  # noqa: ANN202 — a module object
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scripts_reset_delegates_to_the_guarded_command(monkeypatch: pytest.MonkeyPatch):
    module = _load_script("reset")
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "app", lambda args: calls.append(list(args)))

    module.main()

    assert calls == [["reset"]], "the script must go through the CLI, prompt and all"


def test_scripts_reset_no_longer_deletes_anything_itself():
    source = (REPO_ROOT / "scripts" / "reset.py").read_text(encoding="utf-8")

    assert "rmtree" not in source
    assert "./data" not in source
