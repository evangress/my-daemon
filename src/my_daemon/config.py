# SPDX-License-Identifier: Apache-2.0
"""Layered configuration: defaults → config.yaml → environment variables.

Env var overrides use the prefix ``MY_DAEMON_`` and double-underscore nesting,
e.g. ``MY_DAEMON_LLM__MODEL=claude-sonnet-4-6``.

The Anthropic API key is **never** read from config.yaml. It resolves through
:mod:`my_daemon.secrets`: a real environment variable, then the OS credential
store (Windows Credential Manager / macOS Keychain / Linux Secret Service),
then a legacy plaintext ``.env``. Only the last of those touches disk in the
clear, so :attr:`Settings.api_key_source` records which layer answered and
``daemon doctor`` warns when it was the plaintext one.

Two rules make the daemon safe to run from a scheduler:

* **A config must be found.** :func:`load_settings` raises
  :class:`ConfigNotFoundError` rather than falling back to defaults, because
  "defaults" means an empty vault that nobody owns and state written into
  whatever directory the process happened to start in.
* **Relative paths anchor to the config file's directory, not the CWD.** A
  config at ``/home/evan/dev/my-daemon/config.yaml`` saying ``./data/graph.gpickle``
  always means ``/home/evan/dev/my-daemon/data/graph.gpickle`` — from cron
  (which starts in ``$HOME``), from a Windows scheduled task (which starts in
  ``system32``), from anywhere.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal, get_args

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from my_daemon.paths import (
    CONFIG_FILENAME,
    config_search_paths,
    find_config,
    user_config_dir,
    user_config_path,
)
from my_daemon.secrets import ENV_VAR, KeySource, resolve_api_key

__all__ = [
    "CONFIG_FILENAME",
    "ConfigNotFoundError",
    "RunConfig",
    "Settings",
    "config_search_paths",
    "find_config",
    "load_settings",
    "user_config_dir",
    "user_config_path",
]


class VaultConfig(BaseModel):
    path: Path = Path("~/Documents/Obsidian/MyVault").expanduser()
    # Must stay in sync with config.example.yaml — anything constructing
    # Settings() programmatically would otherwise ingest the daemon's own
    # generated prose and its backup copies as if they were the user's notes.
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [".obsidian", ".trash", "templates", "Agent"]
    )
    # Frontmatter keys consulted for a note's episodic date, in order, matched
    # case-insensitively. Tunable because vaults differ; this list covers the
    # four keys observed in the author's.
    date_frontmatter_keys: list[str] = Field(
        default_factory=lambda: ["occurred_at", "date", "created", "created_at"]
    )
    # When set, an otherwise-undated note whose mtime predates this date takes
    # its date from mtime. Off by default, and deliberately a *cutoff* rather
    # than a birth-time comparison: Python exposes no st_birthtime on Linux, and
    # the assumption belongs somewhere the user can see and challenge it.
    #
    # The value is the earliest bulk-import event for this vault. Anything older
    # than it survived a copy and reflects real authoring; anything at or after
    # it is the copy. Measured on the author's vault: mtime before the import is
    # 0 days from the true date at the median, after it 38 days.
    # `daemon migrate backfill-dates` prints the detected import clusters.
    mtime_trusted_before: date | None = None


class ChunkingConfig(BaseModel):
    max_tokens: int = 512
    overlap_tokens: int = 50
    split_on_headers: list[str] = Field(default_factory=lambda: ["#", "##", "###"])


class EmbeddingsConfig(BaseModel):
    model: str = "BAAI/bge-small-en-v1.5"
    batch_size: int = 32
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    cache_folder: Path = Path("./data/models")
    # Hybrid retrieval: a sparse BM25-style signal is fused with the dense one
    # server-side via Qdrant RRF. Flip off to compare against dense-only.
    hybrid: bool = True
    sparse_model: str = "Qdrant/bm42-all-minilm-l6-v2-attentions"


class QdrantConfig(BaseModel):
    """Where the chunk vectors live.

    Two mutually exclusive modes:

    * **embedded** — set ``path`` (a directory, or the literal ``:memory:``).
      qdrant-client runs the engine in-process against that folder, so a
      default install needs no Docker at all. Single-process only: exactly one
      client may hold the folder at a time.
    * **server** — leave ``path`` unset and point ``url`` at a running Qdrant
      (``docker-compose up -d qdrant``). Required for concurrent access, and
      the better choice for large vaults.

    ``url`` keeps its historical default so a config that says nothing at all
    behaves exactly as it did before embedded mode existed; the default only
    applies when ``path`` was not set explicitly.
    """

    url: str = "http://localhost:6333"
    path: Path | str | None = None
    collection: str = "chunks"

    @model_validator(mode="after")
    def _one_mode_only(self) -> QdrantConfig:
        # `model_fields_set` (not the value) is what makes this fair: a user who
        # sets only `path` should not trip over `url`'s default.
        if "url" in self.model_fields_set and "path" in self.model_fields_set:
            raise ValueError(
                "vector_store.qdrant: set either 'url' (server mode) or 'path' "
                "(embedded mode), not both. Embedded mode runs Qdrant in-process "
                "and needs no Docker; comment out 'url' to use it, or remove "
                "'path' to keep talking to a server."
            )
        return self

    @property
    def is_embedded(self) -> bool:
        return self.path is not None

    @property
    def location(self) -> str:
        """Human-readable target of whichever mode is active."""
        return str(self.path) if self.path is not None else self.url


class VectorStoreConfig(BaseModel):
    backend: Literal["qdrant"] = "qdrant"
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)


class GraphConfig(BaseModel):
    path: Path = Path("./data/graph.gpickle")
    manifest_path: Path = Path("./data/manifest.json")
    expansion_depth: int = 2
    distance_decay: float = 0.5


class RetrievalConfig(BaseModel):
    seed_top_k: int = 8
    context_token_budget: int = 6000
    candidate_pool: int = 3
    # Build the candidate pool by team draft between the seed and expansion
    # rankings instead of by raw score (see `retrieval.interleave`). On by
    # default: an expanded chunk's score is its seed's score decayed by graph
    # distance, so under a pure score sort expansion can never outrank the seed
    # that produced it and the expansion policy is never examined — which is
    # why picks taught the graph nothing. Set false to restore score order.
    interleave: bool = True


class LLMConfig(BaseModel):
    provider: Literal["anthropic"] = "anthropic"
    # Sonnet 4.6 is the cost/quality sweet spot for RAG synthesis (~5x cheaper
    # than Opus, near-Opus quality on this kind of task). Swap to Opus 4.7 for
    # the highest fidelity or Haiku 4.5 for the lowest cost.
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048
    # Reasoning-capable models (e.g. Opus 4.7) reject `temperature`; leave unset for those.
    temperature: float | None = None
    # Background-agent jobs (extract / link / reflect) use this cheaper model.
    # Falls back to `model` when None.
    batch_model: str | None = "claude-haiku-4-5"


class AgentConfig(BaseModel):
    """Background-agent (writeback) jobs: extract, link, reflect."""

    # Master gate. Set true after confirming defaults with `daemon link --dry-run`.
    enabled: bool = False
    folder_name: str = "Agent"
    # Linker thresholds. Strict by design.
    link_apply_cosine: float = 0.85
    link_apply_requires_title_substring: bool = True
    link_suggest_cosine: float = 0.78
    link_max_per_note: int = 5
    tag_apply_min_neighbor_count: int = 4
    # Extractor.
    extract_min_word_count: int = 80
    extract_skip_if_processed_within_hours: int = 18
    # Reflection — themes and the per-run lookback window in days.
    reflect_themes: list[str] = Field(
        default_factory=lambda: ["personality", "projects", "relationships", "themes"]
    )
    reflect_lookback_days: int = 7
    # Safety grace: don't touch a note saved within this many minutes.
    write_grace_minutes: int = 30
    # --- Observer (M4) -------------------------------------------------------
    # Separate gate from `enabled` so a user can run extract/link/reflect for
    # weeks before opting into the heavier, prosier consolidation letter.
    observer_enabled: bool = False
    # Lookback for the weight-evolution replay inside `daemon consolidate`.
    observer_lookback_days: int = 7
    # Cap on how many Louvain communities the observer is allowed to discuss.
    observer_max_communities: int = 8
    # Observer model override. None → batch_model. The structural→prose lift
    # is what this milestone exists for; Opus 4.7 is worth the cost here.
    observer_model: str | None = "claude-opus-4-7"
    # How many prior observer letters to feed back in for continuity.
    observer_prior_letters: int = 4
    # How many letters to surface in the rolling `observer.md` index.
    observer_index_window: int = 8
    # Retention for snapshots automatically created by `daemon consolidate`.
    # Overrides `snapshot.retention_days` for the consolidation path only.
    observer_snapshot_retention_days: int = 14


class HermesConfig(BaseModel):
    """Hermes integration (PLAN-HERMES.md). Two faces over one core adapter.

    Everything defaults *off*: the provider only activates when Hermes is
    configured to load it AND ``provider_enabled`` is true, and no write ever
    happens unless ``allow_write_back`` is flipped — same read-only-for-weeks
    discipline as ``agent.enabled``.
    """

    # --- Memory-provider plugin (Path A, primary) -------------------------
    provider_enabled: bool = False
    # Captures land in a *normal* (ingest-visible) folder, not daemon-owned Agent/.
    capture_folder: str = "Conversations/hermes"
    capture_requires_confirmation: bool = True
    # Below this combined user+assistant length a turn is too thin to capture.
    capture_min_chars: int = 120
    prefetch_budget_chars: int = 4000
    dream_block_budget_chars: int = 3000
    # One-paragraph "who you are / why this memory exists" framing injected at
    # session start above last night's letter. Empty → a sensible built-in default.
    identity: str = ""
    # --- Shared retrieval defaults handed to the agent --------------------
    # Recall is retrieval-only by default: Hermes has its own model and wants
    # grounded, cited context, not an answer composed for it (PLAN-HERMES §4).
    recall_synthesize_default: bool = False
    recall_top_k: int = 8
    allow_write_back: bool = False  # gates endorse + remember (both faces)
    # --- MCP server (Path B, optional portability) ------------------------
    mcp_enabled: bool = False
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8077
    # HTTP bearer token is read from this env var only — never from yaml.
    mcp_auth_token_env: str = "MY_DAEMON_MCP_TOKEN"


class MemoryConfig(BaseModel):
    """Fingerprint recall — "you've been here before".

    Injection and display toggle independently on purpose: enriching the
    model's context and showing you the memory are different acts, and you
    may want one without the other.
    """

    recall_enabled: bool = True
    inject_into_context: bool = True
    show_to_user: bool = True
    min_score: float = 0.15
    top_k: int = 3
    lookback_days: int = 180
    max_df_ratio: float = 0.25


class FeedbackConfig(BaseModel):
    db_path: Path = Path("./data/feedback.db")
    # Gates graph reinforcement from an *explicit* pick (a GUI click, `daemon
    # select`). Separate from `hermes.allow_write_back`, which gates ambient
    # capture and Hermes' implicit soft-reinforcement — pressing a button is
    # not the same act as the daemon deciding to write on your behalf.
    reinforce_enabled: bool = True


class SnapshotConfig(BaseModel):
    """Where snapshot bundles live and how long to keep them.

    A snapshot bundle freezes the vector + graph + feedback state so heavy
    analysis (M3) and the observer LLM (M4) can run against a stable copy
    without contaminating live retrieval. Bundles are deliberately on the
    local filesystem — no remote-storage indirection.
    """

    dir: Path = Path("./data/snapshots")
    retention_days: int = 14


class BackupConfig(BaseModel):
    """Where `daemon backup` writes, and where `daemon restore` looks.

    Deliberately *outside* ``./data``: a backup that lives inside the tree
    `daemon reset` clears is not a backup. Vectors are never included — they
    are re-derivable from the vault with `daemon ingest --full`, and excluding
    them is what keeps a bundle small enough to actually be taken often.
    """

    dir: Path = Path("./backups")


class ConsolidationConfig(BaseModel):
    # --- Emergent themes (M-mem-7) --------------------------------------
    cluster_themes: bool = True
    min_cluster_size: int = 3
    # Cosine above which a new cluster is judged the *same* theme as an
    # existing one and inherits its id and label. Label stability matters more
    # than partition stability.
    theme_match_threshold: float = 0.60
    # Only new clusters are named, so this bounds the nightly LLM spend.
    max_new_themes_per_run: int = 5
    theme_query_limit: int = 4000
    """Where M3 structural + weight-evolution reports persist.

    One subdirectory per snapshot id: ``<out_dir>/<snapshot_id>/structural.json``
    and ``weight_evolution.json``. The M4 observer reads from the same place.
    """

    out_dir: Path = Path("./data/consolidation")
    # Default lookback for `daemon analyze` simulate_evolution. Per-run override
    # via the CLI flag.
    simulate_lookback_days: int = 7
    # How many entries to keep in each ranked-list section of a report.
    max_communities: int = 8
    max_bridging_notes: int = 10
    max_bridge_edges: int = 20
    max_orphans: int = 20
    max_dangling: int = 20
    max_warm_edges: int = 20
    # Sample size for `nx.betweenness_centrality(k=...)`. Full BC is O(VE); 200
    # is a reasonable default and matches the M3 plan.
    betweenness_sample_k: int = 200


class RunConfig(BaseModel):
    """`daemon run` — the foreground supervisor.

    Three knobs, and each is a promise about latency:

    * ``debounce_seconds`` — how long the vault must be *quiet* before an
      incremental ingest runs. Not a rate limit: the timer restarts on every
      new change, so a sync client rewriting a hundred files produces one
      ingest after it finishes rather than one in the middle of it.
    * ``consolidate_at`` — local wall-clock ``HH:MM`` for the nightly
      consolidation. ``null`` turns it off. The job still obeys both writeback
      gates (``agent.enabled`` and ``agent.observer_enabled``); with either
      closed the supervisor logs the reason once and stops mentioning it.
    * ``heartbeat_minutes`` — cadence of the "still alive" log line. ``0``
      silences it.
    """

    debounce_seconds: float = 5.0
    consolidate_at: str | None = "03:00"
    heartbeat_minutes: float = 15.0

    @property
    def consolidate_time(self) -> time | None:
        """``consolidate_at`` as a :class:`datetime.time`, or None when off."""
        if self.consolidate_at is None or not self.consolidate_at.strip():
            return None
        try:
            return datetime.strptime(self.consolidate_at.strip(), "%H:%M").time()
        except ValueError as exc:
            raise ValueError(
                f"run.consolidate_at must be 24-hour 'HH:MM' (got {self.consolidate_at!r}); "
                "set it to null to turn the nightly consolidation off."
            ) from exc

    @model_validator(mode="after")
    def _validate(self) -> RunConfig:
        # Parse at load time, not at 03:00 six weeks from now inside a
        # background process whose log nobody is reading. The value is
        # discarded; the parse is the point.
        _ = self.consolidate_time
        if self.debounce_seconds < 0:
            raise ValueError("run.debounce_seconds must not be negative")
        if self.heartbeat_minutes < 0:
            raise ValueError("run.heartbeat_minutes must not be negative (0 disables it)")
        return self


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Settings(BaseSettings):
    """Top-level settings. Resolve via :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="MY_DAEMON_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    vault: VaultConfig = Field(default_factory=VaultConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)

    anthropic_api_key: str | None = None

    # Which layer supplied the key above. Carried so `daemon doctor` can tell
    # the user their secret is sitting in a plaintext file — the value alone
    # cannot reveal that. See :mod:`my_daemon.secrets`.
    api_key_source: KeySource = "missing"

    # The file `load_settings` actually resolved — the anchor every relative
    # path in this object was made absolute against. None when a Settings was
    # constructed programmatically (tests, library callers).
    config_path: Path | None = None


class ConfigNotFoundError(FileNotFoundError):
    """No ``config.yaml`` in any searched location.

    Subclasses :class:`FileNotFoundError` so anything already catching that
    keeps working, but it is a *typed* error the CLI can render as a one-line
    refusal instead of a traceback — and, crucially, instead of the old silent
    fall-through to defaults that let a scheduled job report success against a
    vault that does not exist.
    """

    def __init__(self, searched: list[Path]) -> None:
        self.searched = list(searched)
        locations = "\n".join(f"  - {p}" for p in self.searched)
        super().__init__(
            "no config.yaml found — run `daemon init` here, or "
            "`daemon init --user` to create one in your user config directory.\n"
            f"searched:\n{locations}"
        )


# qdrant-client's in-memory sentinel. Looks path-shaped, is not a path.
_EMBEDDED_MEMORY = ":memory:"


def _anchor(value: Path, root: Path) -> Path:
    """Make one config-supplied path absolute.

    ``~`` first (a user writing ``~/notes`` means their home, not a directory
    literally named ``~`` under the config), then anchor anything still
    relative to the config's own directory. Absolute paths pass through
    untouched.
    """
    expanded = value.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path(os.path.normpath(root / expanded))


def _annotation_allows_path(annotation: object) -> bool:
    return annotation is Path or Path in get_args(annotation)


def _anchor_model_paths(model: BaseModel, root: Path) -> None:
    """Recursively rewrite every relative path field to be absolute.

    Done on the *constructed* Settings rather than on the raw yaml so that
    ``MY_DAEMON_*`` env overrides get anchored too — an override is just as
    likely to be relative, and just as broken from cron if it isn't resolved.

    ``vector_store.qdrant.path`` is annotated ``Path | str | None`` and pydantic
    keeps a yaml string as a ``str``, hence the annotation check: it is a real
    path and has to be anchored, while ``:memory:`` must not be.
    """
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            _anchor_model_paths(value, root)
        elif isinstance(value, Path):
            setattr(model, name, _anchor(value, root))
        elif (
            isinstance(value, str)
            and value != _EMBEDDED_MEMORY
            and _annotation_allows_path(field.annotation)
        ):
            setattr(model, name, str(_anchor(Path(value), root)))


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Resolve a config file, load it, and merge env overrides on top.

    Search order (first hit wins, see :func:`my_daemon.paths.config_search_paths`):
    ``config_path`` / ``--config`` → ``$MY_DAEMON_CONFIG`` → ``./config.yaml``
    → the user config dir.

    Raises :class:`ConfigNotFoundError` when none of them exist. Every relative
    path in the result is anchored to the resolved config's directory.
    """
    searched = config_search_paths(config_path)
    path = find_config(config_path)
    if path is None:
        raise ConfigNotFoundError(searched)

    root = Path(os.path.abspath(path)).parent

    # Captured *before* load_dotenv, because dotenv merges `.env` into
    # os.environ and after that a plaintext key is indistinguishable from a
    # properly-stored one — which is exactly the distinction the user needs.
    real_env_key = os.environ.get(ENV_VAR)

    # `.env` lives next to the config. `override=False` means a value already in
    # the real environment wins, which is what we want when the OS env var was
    # set persistently in a shell profile. The CWD copy is still honored second
    # so an interactive shell in a checkout keeps working when the active
    # config lives elsewhere. This is for the `MY_DAEMON_*` overrides; the API
    # key is resolved separately below.
    load_dotenv(dotenv_path=root / ".env", override=False)
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    # API key is secret-store-only — never read from yaml, never written back.
    # Order: real env var → OS credential store → legacy plaintext `.env`.
    resolved = resolve_api_key(
        env_value=real_env_key,
        dotenv_paths=[root / ".env", Path.cwd() / ".env"],
    )
    data["anthropic_api_key"] = resolved.value
    data["api_key_source"] = resolved.source

    settings = Settings(**data)
    _anchor_model_paths(settings, root)
    settings.config_path = path
    return settings
