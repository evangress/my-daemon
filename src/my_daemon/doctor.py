# SPDX-License-Identifier: Apache-2.0
"""Preflight checks: what `daemon doctor` reports, and what the commands catch.

Every check here exists because some failure used to surface as a traceback in
the middle of work the user had already paid for. Qdrant down printed
``[Errno 111]`` out of retrieval; a missing API key was discovered *after* a
full embed+retrieve; a mistyped vault path reported "Notes scanned 0" and
exited 0. The checks are ordered the way the daemon itself depends on them, so
the first failure is usually the cause of the rest.

Two rules shape the implementation:

* **Nothing here may be expensive.** In particular the collection's vector
  dimension is compared against a dimension read from the *model cache on
  disk* (``1_Pooling/config.json``), never by importing torch and loading a
  130MB model — a doctor that costs a download defeats its own purpose. When
  the cache is cold the dimension is reported as unverified rather than
  guessed.
* **Nothing here may leave state behind.** The embedded vector store takes an
  exclusive lock on its folder; the runner closes it, so `doctor` never wedges
  the command you run next.

The same module owns the error translation used inline by ``query`` / ``ask``
/ ``search`` / ``ingest``, so the remediation text a user sees mid-command and
the one `doctor` prints are the same sentence, written once.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from my_daemon.config import ConfigNotFoundError, Settings, load_settings
from my_daemon.stores.db import SCHEMA_VERSION as DB_SCHEMA_VERSION
from my_daemon.stores.db import schema_version as db_schema_version
from my_daemon.stores.graph import GraphCorruptError, GraphStore
from my_daemon.stores.vector import DENSE_NAME, LocalStoreLockedError, VectorStore

PASS = "pass"
WARN = "warn"
FAIL = "fail"

#: How long to wait on the reachability probe. Long enough for a container that
#: is up, short enough that a doctor run against a dead host still feels instant.
REACHABILITY_TIMEOUT = 2.0

#: Rough on-disk size of the default embedding model, quoted in the cold-cache
#: warning so "first ingest is slow" is a number rather than a surprise.
MODEL_DOWNLOAD_SIZE = "~130MB"


@dataclass(frozen=True)
class CheckResult:
    """One line of the doctor report.

    ``hint`` is the remediation — what to type next — and is only rendered for
    non-passing checks, which is why it can be empty.
    """

    name: str
    status: str
    detail: str
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


# ---------------------------------------------------------------------------
# shared remediation text
# ---------------------------------------------------------------------------


def qdrant_unreachable_hint(settings: Settings) -> str:
    """The one sentence both `doctor` and the inline preflights print."""

    qdrant = settings.vector_store.qdrant
    if qdrant.is_embedded:
        return (
            f"Embedded Qdrant lives at {qdrant.path}; check the folder is writable, "
            "or point vector_store.qdrant at a server 'url' instead."
        )
    return (
        f"Is `docker compose up -d` running at {qdrant.url}? "
        "Start it, or switch vector_store.qdrant to an embedded 'path' (no Docker needed)."
    )


def connection_error_types() -> tuple[type[BaseException], ...]:
    """Exception types that mean "the vector store was not there".

    Discovered lazily and defensively: importing ``qdrant_client`` (and httpx
    behind it) at CLI import time costs startup on *every* command, and a
    missing optional transport must never turn error handling into a second
    error.
    """

    types: list[type[BaseException]] = [ConnectionError, TimeoutError]
    with contextlib.suppress(Exception):
        from qdrant_client.http.exceptions import ResponseHandlingException

        types.append(ResponseHandlingException)
    with contextlib.suppress(Exception):
        import httpx

        types.append(httpx.TransportError)
    return tuple(types)


def store_error_message(settings: Settings, exc: BaseException) -> str:
    """Render a store failure as something a person can act on."""

    if isinstance(exc, LocalStoreLockedError):
        return str(exc)
    qdrant = settings.vector_store.qdrant
    return (
        f"Cannot reach the vector store at {qdrant.location} ({exc}).\n"
        f"{qdrant_unreachable_hint(settings)}"
    )


def server_reachable(url: str, timeout: float = REACHABILITY_TIMEOUT) -> bool:
    """True when *something* answers HTTP at ``url``.

    An HTTP error status still proves reachability — the point of this probe is
    to separate "the container is down" from every other possible failure, and
    a 404 from a live Qdrant is emphatically not the former.
    """

    try:
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 — configured url
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# model cache — the cheap route to the embedder's dimension
# ---------------------------------------------------------------------------


def find_cached_model(cache_folder: Path | None, model_name: str) -> Path | None:
    """The on-disk directory holding ``model_name``, or None if it is not cached.

    Handles both layouts sentence-transformers has used: the HF hub cache
    (``models--org--name/snapshots/<rev>/``) and the older flat directory
    (``org_name/``). Pure filesystem — no torch, no network, no model load.
    """

    if cache_folder is None:
        return None
    root = Path(cache_folder).expanduser()
    if not root.is_dir():
        return None

    hub_dir = root / ("models--" + model_name.replace("/", "--"))
    snapshots = hub_dir / "snapshots"
    if snapshots.is_dir():
        revisions = [p for p in snapshots.iterdir() if p.is_dir()]
        if revisions:
            # Newest revision: a re-download leaves the old one in place.
            return max(revisions, key=lambda p: p.stat().st_mtime)

    flat_names = [
        model_name.replace("/", "_"),
        "sentence-transformers_" + model_name.split("/")[-1],
        model_name.split("/")[-1],
    ]
    for name in flat_names:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def dim_from_model_dir(model_dir: Path) -> int | None:
    """Read the dense dimension out of a cached model's own JSON.

    ``1_Pooling/config.json`` is the authority for a sentence-transformers
    model (it is what the pooling layer actually emits); ``config.json``'s
    ``hidden_size`` is the fallback for a bare transformer checkpoint.
    """

    pooling = model_dir / "1_Pooling" / "config.json"
    for path, key in (
        (pooling, "word_embedding_dimension"),
        (model_dir / "config.json", "hidden_size"),
    ):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError):
            continue
        if isinstance(value, int):
            return value
    return None


def expected_embedding_dim(settings: Settings) -> int | None:
    """The configured embedder's dimension, or None if it cannot be known cheaply."""

    model_dir = find_cached_model(settings.embeddings.cache_folder, settings.embeddings.model)
    if model_dir is None:
        return None
    return dim_from_model_dir(model_dir)


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def check_config(explicit: Path | None) -> tuple[CheckResult, Settings | None]:
    """Which config resolved — and, when none did, where we looked.

    Deliberately does *not* go through the CLI's `_load()`: that exits the
    process, and here the miss is the finding, not the end of the report.
    """

    try:
        settings = load_settings(explicit)
    except ConfigNotFoundError as exc:
        # Detail stays short and hint carries the paths: the table wraps, and a
        # path broken across a column boundary is not a path you can paste.
        searched = "\n".join(f"    - {p}" for p in exc.searched)
        return (
            CheckResult(
                "config",
                FAIL,
                "no config.yaml found in any searched location",
                hint=(
                    f"searched:\n{searched}\n"
                    "    run `daemon init` here, or `daemon init --user` to create one "
                    "in your user config directory."
                ),
            ),
            None,
        )
    return CheckResult("config", PASS, f"resolved {settings.config_path}"), settings


def check_vault(settings: Settings) -> CheckResult:
    path = settings.vault.path
    if not path.exists():
        return CheckResult(
            "vault",
            FAIL,
            f"{path} does not exist",
            hint="fix `vault.path` in your config, or point it at the folder Obsidian opens.",
        )
    if not path.is_dir():
        return CheckResult(
            "vault",
            FAIL,
            f"{path} is not a directory",
            hint="`vault.path` must be the vault folder itself, not a file inside it.",
        )
    excluded = set(settings.vault.exclude_dirs)
    count = sum(
        1 for md in path.rglob("*.md") if not (excluded & set(md.relative_to(path).parts[:-1]))
    )
    detail = f"{path} — {count} markdown files"
    if count == 0:
        return CheckResult(
            "vault",
            WARN,
            detail,
            hint="the vault exists but holds no notes the daemon would read.",
        )
    return CheckResult("vault", PASS, detail)


def check_vector_store(settings: Settings) -> tuple[CheckResult, VectorStore | None]:
    """Reachability, in whichever mode is configured.

    Returns the *open* store on success so the collection check can reuse the
    connection; the caller must ``close()`` it — embedded mode holds an
    exclusive folder lock and a doctor that kept it would break the next
    command it was run to diagnose.
    """

    qdrant = settings.vector_store.qdrant
    if not qdrant.is_embedded and not server_reachable(qdrant.url):
        return (
            CheckResult(
                "vector store",
                FAIL,
                f"no answer from {qdrant.url}",
                hint=qdrant_unreachable_hint(settings),
            ),
            None,
        )

    # dim=1 is never written anywhere: this store is only ever asked to open a
    # connection and describe what already exists.
    store = VectorStore.from_config(qdrant, dim=1, hybrid=settings.embeddings.hybrid)
    try:
        store._client_()
    except LocalStoreLockedError as exc:
        store.close()
        return (
            CheckResult(
                "vector store",
                FAIL,
                str(exc),
                hint="stop the other my-daemon process, or move to a server `url`.",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — every cause is a reachability finding
        store.close()
        return (
            CheckResult(
                "vector store",
                FAIL,
                f"{qdrant.location}: {exc}",
                hint=qdrant_unreachable_hint(settings),
            ),
            None,
        )

    mode = "embedded" if qdrant.is_embedded else "server"
    return CheckResult("vector store", PASS, f"{mode} — {store.location} reachable"), store


def _dense_dim(vectors_config: object) -> int | None:
    """Pull the dense size out of either collection schema shape."""

    # Named-vector (hybrid) configs are dicts keyed by slot; the legacy
    # single-slot config is a bare VectorParams.
    params = vectors_config.get(DENSE_NAME) if isinstance(vectors_config, dict) else vectors_config
    size = getattr(params, "size", None)
    return int(size) if isinstance(size, int) else None


def check_collection(settings: Settings, store: VectorStore | None) -> CheckResult:
    collection = settings.vector_store.qdrant.collection
    if store is None:
        return CheckResult(
            "collection",
            WARN,
            "skipped — the vector store is not reachable",
            hint="fix the vector store first; this check needs a connection.",
        )
    try:
        client = store._client_()
        names = {c.name for c in client.get_collections().collections}
        if collection not in names:
            return CheckResult(
                "collection",
                WARN,
                f"'{collection}' does not exist yet",
                hint="run `daemon ingest` to create and populate it.",
            )
        info = client.get_collection(collection_name=collection)
        found = _dense_dim(info.config.params.vectors)
    except Exception as exc:  # noqa: BLE001 — a describe failure is a finding
        return CheckResult(
            "collection",
            FAIL,
            f"could not describe '{collection}': {exc}",
            hint=qdrant_unreachable_hint(settings),
        )

    expected = expected_embedding_dim(settings)
    if found is None:
        return CheckResult(
            "collection",
            WARN,
            f"'{collection}' exists but declares no dense vector",
            hint="run `daemon ingest --full` to recreate it with the current schema.",
        )
    if expected is None:
        return CheckResult(
            "collection",
            PASS,
            f"'{collection}' — dense dim {found} (not verified: model not cached)",
        )
    if expected != found:
        return CheckResult(
            "collection",
            FAIL,
            f"'{collection}' has dense dim {found}, but {settings.embeddings.model} emits {expected}",
            hint="run `daemon reset` then `daemon ingest --full` to rebuild at the new dimension.",
        )
    return CheckResult(
        "collection", PASS, f"'{collection}' — dense dim {found} matches the embedder"
    )


def check_api_key(settings: Settings) -> CheckResult:
    if settings.anthropic_api_key:
        return CheckResult("api key", PASS, "ANTHROPIC_API_KEY present in the environment")
    return CheckResult(
        "api key",
        WARN,
        "ANTHROPIC_API_KEY is not set",
        hint=(
            "needed by `query`/`ask` synthesis and by `extract`, `reflect`, `consolidate`; "
            "retrieval (`search`, `ingest`, `query --no-llm`) works without it. "
            "Set it in the environment or in the .env beside your config."
        ),
    )


def check_model_cache(settings: Settings) -> CheckResult:
    model = settings.embeddings.model
    folder = settings.embeddings.cache_folder
    model_dir = find_cached_model(folder, model)
    if model_dir is None:
        return CheckResult(
            "model cache",
            WARN,
            f"{model} is not cached under {folder}",
            hint=f"cold cache — the first `daemon ingest` will download {MODEL_DOWNLOAD_SIZE}.",
        )
    dim = dim_from_model_dir(model_dir)
    suffix = f" (dim {dim})" if dim is not None else ""
    return CheckResult("model cache", PASS, f"{model} cached at {model_dir}{suffix}")


def check_db_schema(settings: Settings) -> CheckResult:
    db_path = settings.feedback.db_path
    if not db_path.is_file():
        return CheckResult(
            "state db",
            PASS,
            f"{db_path} not created yet — it will start at v{DB_SCHEMA_VERSION}",
        )
    found = db_schema_version(db_path)
    if found > DB_SCHEMA_VERSION:
        return CheckResult(
            "state db",
            FAIL,
            f"v{found} is newer than this build understands (v{DB_SCHEMA_VERSION})",
            hint="upgrade my-daemon; a database is never migrated downwards.",
        )
    if found < DB_SCHEMA_VERSION:
        return CheckResult(
            "state db",
            WARN,
            f"v{found}, behind the current v{DB_SCHEMA_VERSION}",
            hint="run `daemon migrate db` to apply the pending migrations.",
        )
    return CheckResult("state db", PASS, f"v{found} — current")


def check_graph(settings: Settings) -> CheckResult:
    path = settings.graph.path
    if not path.is_file():
        return CheckResult(
            "graph",
            WARN,
            f"{path} does not exist yet",
            hint="run `daemon ingest` to build it.",
        )
    store = GraphStore(path=path)
    try:
        store.load()
    except GraphCorruptError as exc:
        return CheckResult(
            "graph",
            FAIL,
            str(exc),
            hint="run `daemon ingest --full` to rebuild it from the vault.",
        )
    except Exception as exc:  # noqa: BLE001 — anything else is still "cannot read it"
        return CheckResult(
            "graph",
            FAIL,
            f"{path}: {exc}",
            hint="run `daemon ingest --full` to rebuild it from the vault.",
        )
    stats = store.stats()
    return CheckResult(
        "graph",
        PASS,
        f"{stats.note_count} notes, {stats.tag_count} tags, {stats.edge_count} edges",
    )


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


def run_checks(
    explicit: Path | None = None, *, settings: Settings | None = None
) -> list[CheckResult]:
    """Every check, in dependency order.

    ``settings`` short-circuits resolution for callers that already hold one
    (and for tests); otherwise the config check does the resolving and an
    unresolvable config ends the report right there — nothing below it can
    mean anything without one.
    """

    if settings is None:
        config_result, settings = check_config(explicit)
        if settings is None:
            return [config_result]
    else:
        where = settings.config_path or "(supplied by the caller)"
        config_result = CheckResult("config", PASS, f"resolved {where}")

    results = [config_result, check_vault(settings)]
    store_result, store = check_vector_store(settings)
    results.append(store_result)
    try:
        results.append(check_collection(settings, store))
    finally:
        if store is not None:
            store.close()
    results.extend(
        [
            check_api_key(settings),
            check_model_cache(settings),
            check_db_schema(settings),
            check_graph(settings),
        ]
    )
    return results
