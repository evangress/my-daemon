# SPDX-License-Identifier: Apache-2.0
"""`daemon doctor` — the preflight that answers "why did that just fail?".

Every failure mode here was a raw traceback (or worse, a cheerful exit 0) at
some point: Qdrant down printed `Errno 111` out of the middle of retrieval, a
missing API key surfaced only after a full embed+retrieve, and a bad vault path
reported "Notes scanned 0" as if that were an answer.

The checks are written to be runnable *individually* against a hand-built
Settings — that is what keeps them cheap enough to also run inline before
`query` / `ingest`. The CLI-level tests use a real `config.yaml` on a tmp path,
because "which config did you actually resolve?" is check #1 and stubbing
`_load()` would answer it by fiat.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon import doctor
from my_daemon.cli import app
from my_daemon.config import Settings
from my_daemon.stores.db import SCHEMA_VERSION
from my_daemon.stores.db import migrate as db_migrate

runner = CliRunner()

# A port nothing listens on: connecting must be refused, not hang.
DEAD_URL = "http://127.0.0.1:9"


_BOX_CHARS = "┏┓┗┛━─│┃┡┩┠┨╇┼├┤┬┴╭╮╰╯╺╸"


def _squash(text: str) -> str:
    stripped = text.translate({ord(c): " " for c in _BOX_CHARS})
    return " ".join(stripped.split())


def _by_name(results: list[doctor.CheckResult]) -> dict[str, doctor.CheckResult]:
    return {r.name: r for r in results}


@pytest.fixture
def settings(tmp_path: Path, vault_root: Path) -> Settings:
    """Healthy-by-default settings: real vault fixture, embedded Qdrant, tmp state."""
    s = Settings()
    s.vault.path = vault_root
    s.vector_store.qdrant.url = "http://localhost:6333"
    s.vector_store.qdrant.path = tmp_path / "qdrant"
    s.graph.path = tmp_path / "data" / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "data" / "manifest.json"
    s.feedback.db_path = tmp_path / "data" / "feedback.db"
    s.embeddings.cache_folder = tmp_path / "data" / "models"
    s.embeddings.hybrid = False
    s.anthropic_api_key = "sk-ant-test"
    return s


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No inherited config, home, CWD or API key — nothing may pass by accident."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.delenv("MY_DAEMON_CONFIG", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.chdir(work)
    return work


def _write_config(work: Path, vault: Path, **extra: str) -> Path:
    body = (
        f"vault:\n  path: {vault}\n"
        "vector_store:\n  qdrant:\n    path: ./data/qdrant\n"
        "graph:\n  path: ./data/graph.gpickle\n  manifest_path: ./data/manifest.json\n"
        "feedback:\n  db_path: ./data/feedback.db\n"
        "embeddings:\n  cache_folder: ./data/models\n  hybrid: false\n"
    )
    for block in extra.values():
        body += block
    path = work / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_config_is_a_finding_not_a_crash(isolated_env: Path):
    """`doctor` cannot use `_load()`'s exit path — the miss *is* the report."""
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    squashed = _squash(result.output)
    assert "no config.yaml found" in squashed
    assert str(isolated_env / "config.yaml") in squashed


def test_config_check_names_the_file_that_resolved(isolated_env: Path, vault_root: Path):
    path = _write_config(isolated_env, vault_root)

    result, settings = doctor.check_config(None)

    assert result.status == doctor.PASS
    assert str(path) in result.detail
    assert settings is not None
    assert settings.vault.path == vault_root


def test_doctor_exits_zero_on_a_healthy_vault_and_says_so(
    isolated_env: Path, vault_root: Path
):
    _write_config(isolated_env, vault_root)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    squashed = _squash(result.output)
    assert "vault" in squashed
    # Six markdown notes live in the fixture vault.
    assert "6 markdown" in squashed


def test_doctor_exits_one_when_the_vault_is_missing(isolated_env: Path, tmp_path: Path):
    _write_config(isolated_env, tmp_path / "nope")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "nope" in _squash(result.output)


# ---------------------------------------------------------------------------
# vault
# ---------------------------------------------------------------------------


def test_vault_check_counts_markdown_files(settings: Settings):
    result = doctor.check_vault(settings)

    assert result.status == doctor.PASS
    assert "6 markdown" in result.detail


def test_vault_check_fails_when_the_path_is_a_file(settings: Settings, tmp_path: Path):
    victim = tmp_path / "not-a-vault.md"
    victim.write_text("hi", encoding="utf-8")
    settings.vault.path = victim

    result = doctor.check_vault(settings)

    assert result.status == doctor.FAIL
    assert "not a directory" in result.detail


def test_vault_check_fails_when_the_path_is_absent(settings: Settings, tmp_path: Path):
    settings.vault.path = tmp_path / "ghost"

    result = doctor.check_vault(settings)

    assert result.status == doctor.FAIL
    assert result.hint


# ---------------------------------------------------------------------------
# vector store
# ---------------------------------------------------------------------------


def test_embedded_store_is_reachable_and_the_lock_is_released(settings: Settings):
    result, store = doctor.check_vector_store(settings)
    assert result.status == doctor.PASS, result.detail
    assert store is not None
    store.close()

    # The whole point of close(): a second doctor run (or any command) must
    # still be able to take the embedded store's exclusive folder lock.
    again, store2 = doctor.check_vector_store(settings)
    assert again.status == doctor.PASS, again.detail
    assert store2 is not None
    store2.close()


def test_embedded_store_reports_a_held_lock(settings: Settings):
    _, holder = doctor.check_vector_store(settings)
    assert holder is not None
    try:
        result, store = doctor.check_vector_store(settings)
    finally:
        holder.close()

    assert result.status == doctor.FAIL
    assert store is None
    assert "already open" in result.detail


def test_server_mode_reports_an_unreachable_qdrant_with_the_compose_hint(
    settings: Settings,
):
    settings.vector_store.qdrant.path = None
    settings.vector_store.qdrant.url = DEAD_URL

    result, store = doctor.check_vector_store(settings)

    assert result.status == doctor.FAIL
    assert store is None
    assert "docker compose up -d" in result.hint
    assert DEAD_URL in result.hint


# ---------------------------------------------------------------------------
# collection + dimension
# ---------------------------------------------------------------------------


def _make_collection(settings: Settings, dim: int) -> None:
    from my_daemon.stores.vector import VectorStore

    store = VectorStore.from_config(settings.vector_store.qdrant, dim=dim, hybrid=False)
    store.ensure_collection()
    store.close()


def test_absent_collection_is_a_warning_not_a_failure(settings: Settings):
    _, store = doctor.check_vector_store(settings)
    assert store is not None
    try:
        result = doctor.check_collection(settings, store)
    finally:
        store.close()

    assert result.status == doctor.WARN
    assert "daemon ingest" in result.hint


def test_collection_dim_is_read_without_loading_the_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    _make_collection(settings, dim=384)

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("doctor must not import the embedding model")

    monkeypatch.setattr("my_daemon.embeddings.Embedder._ensure_loaded", _explode)
    monkeypatch.setattr(doctor, "expected_embedding_dim", lambda _s: 384)

    _, store = doctor.check_vector_store(settings)
    assert store is not None
    try:
        result = doctor.check_collection(settings, store)
    finally:
        store.close()

    assert result.status == doctor.PASS
    assert "384" in result.detail


def test_a_dim_mismatch_is_a_hard_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    _make_collection(settings, dim=384)
    monkeypatch.setattr(doctor, "expected_embedding_dim", lambda _s: 768)

    _, store = doctor.check_vector_store(settings)
    assert store is not None
    try:
        result = doctor.check_collection(settings, store)
    finally:
        store.close()

    assert result.status == doctor.FAIL
    assert "384" in result.detail and "768" in result.detail
    assert "ingest --full" in result.hint


def test_an_unknowable_dim_is_reported_not_guessed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    """A cold model cache means we cannot know the embedder's dim without a
    130MB download — say so rather than pass or fail on a guess."""
    _make_collection(settings, dim=384)
    monkeypatch.setattr(doctor, "expected_embedding_dim", lambda _s: None)

    _, store = doctor.check_vector_store(settings)
    assert store is not None
    try:
        result = doctor.check_collection(settings, store)
    finally:
        store.close()

    assert result.status == doctor.PASS
    assert "not verified" in result.detail


# ---------------------------------------------------------------------------
# model cache + expected dim
# ---------------------------------------------------------------------------


def test_expected_dim_comes_from_the_hub_cache_layout(tmp_path: Path):
    snapshot = (
        tmp_path / "models--BAAI--bge-small-en-v1.5" / "snapshots" / "abc123"
    )
    (snapshot / "1_Pooling").mkdir(parents=True)
    (snapshot / "1_Pooling" / "config.json").write_text(
        json.dumps({"word_embedding_dimension": 384}), encoding="utf-8"
    )

    found = doctor.find_cached_model(tmp_path, "BAAI/bge-small-en-v1.5")

    assert found == snapshot
    assert doctor.dim_from_model_dir(snapshot) == 384


def test_expected_dim_falls_back_to_hidden_size(tmp_path: Path):
    model_dir = tmp_path / "BAAI_bge-small-en-v1.5"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text(
        json.dumps({"hidden_size": 768}), encoding="utf-8"
    )

    found = doctor.find_cached_model(tmp_path, "BAAI/bge-small-en-v1.5")

    assert found == model_dir
    assert doctor.dim_from_model_dir(model_dir) == 768


def test_a_cold_cache_is_a_warning_naming_the_download(settings: Settings):
    result = doctor.check_model_cache(settings)

    assert result.status == doctor.WARN
    assert "130MB" in result.hint
    assert settings.embeddings.model in result.detail


def test_a_warm_cache_passes(settings: Settings):
    snapshot = (
        settings.embeddings.cache_folder
        / "models--BAAI--bge-small-en-v1.5"
        / "snapshots"
        / "abc123"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text(
        json.dumps({"hidden_size": 384}), encoding="utf-8"
    )

    result = doctor.check_model_cache(settings)

    assert result.status == doctor.PASS
    assert doctor.expected_embedding_dim(settings) == 384


# ---------------------------------------------------------------------------
# api key
# ---------------------------------------------------------------------------


def test_a_present_key_passes_without_echoing_it(settings: Settings):
    settings.anthropic_api_key = "sk-ant-secret-value"

    result = doctor.check_api_key(settings)

    assert result.status == doctor.PASS
    assert "secret-value" not in result.detail


def test_a_missing_key_warns_and_names_the_commands(settings: Settings):
    settings.anthropic_api_key = None

    result = doctor.check_api_key(settings)

    assert result.status == doctor.WARN
    for command in ("query", "ask", "extract", "reflect", "consolidate"):
        assert command in result.hint
    assert "retrieval" in result.hint


# ---------------------------------------------------------------------------
# state db
# ---------------------------------------------------------------------------


def test_a_current_schema_passes(settings: Settings):
    db_migrate(settings.feedback.db_path)

    result = doctor.check_db_schema(settings)

    assert result.status == doctor.PASS
    assert f"v{SCHEMA_VERSION}" in result.detail


def test_an_absent_db_is_not_a_problem(settings: Settings):
    result = doctor.check_db_schema(settings)

    assert result.status == doctor.PASS
    assert "not created yet" in result.detail


def test_a_stale_schema_warns_with_the_migrate_hint(settings: Settings):
    db_migrate(settings.feedback.db_path, target=1)

    result = doctor.check_db_schema(settings)

    assert result.status == doctor.WARN
    assert "daemon migrate db" in result.hint


def test_a_future_schema_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """A database written by a newer build is not something to migrate *down*."""
    db_migrate(settings.feedback.db_path)
    monkeypatch.setattr(doctor, "DB_SCHEMA_VERSION", SCHEMA_VERSION - 1)

    result = doctor.check_db_schema(settings)

    assert result.status == doctor.FAIL
    assert "newer" in result.detail


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def test_a_missing_graph_warns(settings: Settings):
    result = doctor.check_graph(settings)

    assert result.status == doctor.WARN
    assert "daemon ingest" in result.hint


def test_a_corrupt_graph_fails_by_name(settings: Settings):
    settings.graph.path.parent.mkdir(parents=True, exist_ok=True)
    settings.graph.path.write_bytes(b"not a pickle, not even close")

    result = doctor.check_graph(settings)

    assert result.status == doctor.FAIL
    assert "corrupt" in result.detail


def test_a_real_graph_passes_with_its_size(settings: Settings, vault_root: Path):
    from my_daemon.stores import GraphStore
    from my_daemon.vault import VaultReader
    from my_daemon.vault.chunker import chunk_note

    store = GraphStore(path=settings.graph.path)
    for note in VaultReader(vault_root).read_all():
        store.add_note(note, chunk_ids=[c.id for c in chunk_note(note)])
    store.save()

    result = doctor.check_graph(settings)

    assert result.status == doctor.PASS
    assert "6 notes" in result.detail


# ---------------------------------------------------------------------------
# run_checks — order and exit semantics
# ---------------------------------------------------------------------------


def test_run_checks_runs_every_check_in_order(settings: Settings):
    results = doctor.run_checks(settings=settings)

    assert [r.name for r in results] == [
        "config",
        "vault",
        "vector store",
        "collection",
        "api key",
        "model cache",
        "state db",
        "graph",
    ]


def test_run_checks_stops_after_an_unresolvable_config(isolated_env: Path):
    results = doctor.run_checks(None)

    assert [r.name for r in results] == ["config"]
    assert results[0].status == doctor.FAIL


def test_run_checks_skips_the_collection_when_the_store_is_unreachable(
    settings: Settings,
):
    settings.vector_store.qdrant.path = None
    settings.vector_store.qdrant.url = DEAD_URL

    by_name = _by_name(doctor.run_checks(settings=settings))

    assert by_name["vector store"].status == doctor.FAIL
    assert by_name["collection"].status == doctor.WARN
    assert "skipped" in by_name["collection"].detail
    # A dead Qdrant must not stop the checks that do not need it.
    assert by_name["graph"].status in (doctor.PASS, doctor.WARN)


def test_warnings_alone_do_not_fail_the_run(settings: Settings):
    results = doctor.run_checks(settings=settings)

    assert any(r.status == doctor.WARN for r in results)
    assert not any(r.failed for r in results)


# ---------------------------------------------------------------------------
# inline preflights
# ---------------------------------------------------------------------------


def test_query_translates_a_refused_connection(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
):
    settings.vector_store.qdrant.path = None
    settings.vector_store.qdrant.url = DEAD_URL
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    def _explode(*_a: object, **_k: object) -> None:
        raise ConnectionError("[Errno 111] Connection refused")

    monkeypatch.setattr("my_daemon.cli.build_stores", _explode)

    result = runner.invoke(app, ["query", "anything"])

    assert result.exit_code == 1
    squashed = _squash(result.output)
    assert "docker compose up -d" in squashed
    assert "Traceback" not in result.output


def test_search_translates_the_same_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
):
    settings.vector_store.qdrant.path = None
    settings.vector_store.qdrant.url = DEAD_URL
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    def _explode(*_a: object, **_k: object) -> None:
        raise ConnectionError("[Errno 111] Connection refused")

    monkeypatch.setattr("my_daemon.cli._build_embedder", _explode)

    result = runner.invoke(app, ["search", "anything"])

    assert result.exit_code == 1
    assert "docker compose up -d" in _squash(result.output)


def test_ingest_translates_an_embedded_lock(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
):
    from my_daemon.stores.vector import LocalStoreLockedError

    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    def _explode(*_a: object, **_k: object) -> None:
        raise LocalStoreLockedError("Embedded Qdrant storage at /tmp/q is already open")

    monkeypatch.setattr("my_daemon.cli._build_embedder", _explode)

    result = runner.invoke(app, ["ingest"])

    assert result.exit_code == 1
    assert "already open" in _squash(result.output)
    assert "Traceback" not in result.output


def test_a_graph_lock_timeout_is_not_mistaken_for_a_dead_qdrant(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
):
    """`GraphLockTimeout` is a `TimeoutError`, so it lands in the same except."""
    from my_daemon.stores.graph import GraphLockTimeout

    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    def _explode(*_a: object, **_k: object) -> None:
        raise GraphLockTimeout("timed out waiting for graph.gpickle.lock — held by pid 42")

    monkeypatch.setattr("my_daemon.cli._build_embedder", _explode)

    result = runner.invoke(app, ["ingest"])

    assert result.exit_code != 0
    assert "docker compose" not in result.output
    assert isinstance(result.exception, GraphLockTimeout)


def test_the_qdrant_response_exception_is_in_the_translated_family():
    """The family is discovered lazily so importing the CLI stays cheap."""
    from qdrant_client.http.exceptions import ResponseHandlingException

    assert issubclass(ResponseHandlingException, doctor.connection_error_types())
