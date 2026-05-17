"""Layered configuration: defaults → config.yaml → environment variables.

Env var overrides use the prefix ``MY_DAEMON_`` and double-underscore nesting,
e.g. ``MY_DAEMON_LLM__MODEL=claude-sonnet-4-6``.

The Anthropic API key is **never** read from config.yaml — it comes from the
environment (or a project-local ``.env`` file, which is auto-loaded). This
keeps the key out of any file the user might accidentally commit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VaultConfig(BaseModel):
    path: Path = Path("~/Documents/Obsidian/MyVault").expanduser()
    exclude_dirs: list[str] = Field(default_factory=lambda: [".obsidian", ".trash", "templates"])


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
    url: str = "http://localhost:6333"
    collection: str = "chunks"


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


class FeedbackConfig(BaseModel):
    db_path: Path = Path("./data/feedback.db")


class SnapshotConfig(BaseModel):
    """Where snapshot bundles live and how long to keep them.

    A snapshot bundle freezes the vector + graph + feedback state so heavy
    analysis (M3) and the observer LLM (M4) can run against a stable copy
    without contaminating live retrieval. Bundles are deliberately on the
    local filesystem — no remote-storage indirection.
    """

    dir: Path = Path("./data/snapshots")
    retention_days: int = 14


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
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    anthropic_api_key: str | None = None


def _default_config_path() -> Path:
    explicit = os.environ.get("MY_DAEMON_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.cwd() / "config.yaml"


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from yaml (if present) and merge env overrides on top.

    Loads a project-local ``.env`` file first so ``ANTHROPIC_API_KEY`` (and any
    other env-driven overrides) are honored without requiring the user to
    ``export`` them in every shell.
    """

    # `.env` lives next to the config (project root by default). `override=False`
    # means a value already in the real environment wins, which is what we want
    # when the OS env var was set persistently via `setx` or shell profile.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    path = config_path or _default_config_path()
    data: dict = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    if "vault" in data and "path" in data["vault"]:
        data["vault"]["path"] = str(Path(data["vault"]["path"]).expanduser())

    # API key is environment-only — never read from yaml, never written back.
    data["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY")

    return Settings(**data)
