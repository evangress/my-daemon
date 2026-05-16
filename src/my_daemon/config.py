"""Layered configuration: defaults → config.yaml → environment variables.

Env var overrides use the prefix ``MY_DAEMON_`` and double-underscore nesting,
e.g. ``MY_DAEMON_LLM__MODEL=claude-sonnet-4-6``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
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
    model: str = "claude-opus-4-7"
    max_tokens: int = 2048
    temperature: float = 0.3


class FeedbackConfig(BaseModel):
    db_path: Path = Path("./data/feedback.db")


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
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    anthropic_api_key: str | None = None


def _default_config_path() -> Path:
    explicit = os.environ.get("MY_DAEMON_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.cwd() / "config.yaml"


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from yaml (if present) and merge env overrides on top."""

    path = config_path or _default_config_path()
    data: dict = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    if "vault" in data and "path" in data["vault"]:
        data["vault"]["path"] = str(Path(data["vault"]["path"]).expanduser())

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        data.setdefault("anthropic_api_key", api_key)

    return Settings(**data)
