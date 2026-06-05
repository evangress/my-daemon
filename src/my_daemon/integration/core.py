# SPDX-License-Identifier: Apache-2.0
"""The one core service layer every front end onto My Daemon shares.

Hermes (the memory-provider plugin, ``my_daemon.hermes.provider``) and the
optional MCP server are thin *faces*; all the real work lives here behind plain
methods over already-built stores. This is the seam PLAN-HERMES.md §3 calls
"the ONE adapter over the pipeline":

    recall · recall_block · endorse · remember ·
    latest_dream · dreams · read_dream · neighbors · status

``recall`` defaults to **retrieval-only** (no LLM synthesis): Hermes has its own
model and wants grounded, cited context, not an answer composed for it
(PLAN-HERMES §4). The Anthropic key stays daemon-side — the ``synthesize=True``
path remains for clients that want a one-shot grounded answer (NiceGUI, MCP).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.models import RetrievalResult
from my_daemon.pipeline.ingest import ingest_note
from my_daemon.pipeline.query import QueryEngine, build_retrieval_summary
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores import FeedbackStore, GraphStore, VectorStore
from my_daemon.vault.parser import parse_note
from my_daemon.vault.writer import write_atomic

# Matches the observer pipeline's letter filenames — the "dreams".
_LETTER_RE = re.compile(r"^observer-(\d{4}-\d{2}-\d{2})\.md$")
_PREVIEW_CHARS = 240
_SLUG_MAX = 60


@dataclass
class RecallBlock:
    """A budget-capped markdown context block plus the bookkeeping a write-back
    loop needs: the ``feedback_event_id`` the recall logged and the candidates
    it surfaced (so a later turn can reinforce whichever note the agent used)."""

    text: str
    feedback_event_id: int
    candidates: list[dict] = field(default_factory=list)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:_SLUG_MAX].rstrip("-")) or "capture"


def _preview(text: str, *, limit: int = _PREVIEW_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class DaemonCore:
    """Thin adapter over the existing pipeline. Construct via :func:`build_core`
    (which wires the real stores) or directly with injected stores for tests.

    Write methods (``endorse``, ``remember``) are gated on
    ``settings.hermes.allow_write_back`` so both faces inherit the same
    read-only-by-default discipline, and guarded by a lock because the Hermes
    provider drives capture from a background thread.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        feedback_store: FeedbackStore,
        llm_client: LLMClient,
        sparse_embedder: SparseEmbedder | None = None,
    ) -> None:
        self.s = settings
        self.embedder = embedder
        self.sparse_embedder = sparse_embedder
        self.vector_store = vector_store
        self.graph = graph_store
        self.feedback = feedback_store
        self.llm = llm_client
        self.engine = QueryEngine(
            settings, embedder, vector_store, graph_store, feedback_store, llm_client,
            sparse_embedder=sparse_embedder,
        )
        self._write_lock = threading.Lock()

    # -- read -------------------------------------------------------------

    @property
    def write_enabled(self) -> bool:
        return self.s.hermes.allow_write_back

    def recall(self, query: str, *, top_k: int | None = None, synthesize: bool = False) -> dict:
        """Retrieve (and optionally synthesize) for ``query``.

        Returns the ``feedback_event_id`` the retrieval logged plus ranked
        candidates carrying ``seed_note_path`` — exactly what ``endorse`` needs
        to reinforce the graph path the agent ends up leaning on.
        """

        limit = top_k or self.s.hermes.recall_top_k
        resp = self.engine.ask(query, synthesize=synthesize)
        out: dict = {
            "feedback_event_id": resp.feedback_event_id,
            "latency_ms": resp.latency_ms,
            "candidates": self._candidates_from(resp.retrieval, limit=limit),
        }
        if synthesize:
            out["answer"] = resp.answer
        return out

    def recall_block(
        self, query: str, *, budget_chars: int, top_k: int | None = None
    ) -> RecallBlock:
        """Recall, then render a cited, size-capped markdown block for injection
        into ``prefetch()`` / ``system_prompt_block()``."""

        data = self.recall(query, top_k=top_k, synthesize=False)
        text = self._format_block(query, data["candidates"], budget_chars=budget_chars)
        return RecallBlock(
            text=text,
            feedback_event_id=data["feedback_event_id"],
            candidates=data["candidates"],
        )

    def _candidates_from(self, result: RetrievalResult, *, limit: int) -> list[dict]:
        summary = build_retrieval_summary(result)["ranked"]
        cands: list[dict] = []
        for rank, (entry, rc) in enumerate(zip(summary, result.ranked, strict=True), start=1):
            if rank > limit:
                break
            cands.append(
                {
                    "rank": rank,
                    "chunk_id": entry["chunk_id"],
                    "note_path": entry["note_path"],
                    "heading_path": entry["heading_path"],
                    "score": entry["score"],
                    "preview": _preview(rc.chunk.text),
                    "seed_note_path": entry["seed_note_path"],
                }
            )
        return cands

    def _format_block(self, query: str, candidates: list[dict], *, budget_chars: int) -> str:
        if not candidates:
            return ""
        header = f'Relevant memory for: "{query}"\n'
        parts = [header]
        used = len(header)
        for c in candidates:
            heading = " › ".join(c["heading_path"]) if c["heading_path"] else ""
            loc = f" › {heading}" if heading else ""
            entry = f"\n- `{c['note_path']}`{loc}\n  {c['preview']}\n"
            if used + len(entry) > budget_chars and len(parts) > 1:
                break
            parts.append(entry)
            used += len(entry)
        return "".join(parts).rstrip() + "\n"

    def neighbors(self, note_path: str, *, depth: int = 1) -> dict:
        dists = self.graph.neighbors_within(note_path, depth, weighted=True)
        ordered = sorted(dists.items(), key=lambda kv: kv[1])
        return {
            "note_path": note_path,
            "depth": depth,
            "neighbors": [{"note_path": n, "distance": round(d, 4)} for n, d in ordered],
        }

    def status(self) -> dict:
        g = self.graph.stats()
        try:
            chunks: int | str = self.vector_store.count()
        except Exception as exc:  # noqa: BLE001 — Qdrant may be offline; report, don't crash
            chunks = f"unavailable ({exc})"
        return {
            "vault": str(self.s.vault.path),
            "vector_chunks": chunks,
            "notes": g.note_count,
            "tags": g.tag_count,
            "edges": g.edge_count,
            "write_back": self.write_enabled,
        }

    # -- dreams (observer letters) ----------------------------------------

    def _letter_files(self) -> list[tuple[str, Path]]:
        folder = self.s.vault.path / self.s.agent.folder_name
        if not folder.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for path in folder.iterdir():
            if not path.is_file():
                continue
            m = _LETTER_RE.match(path.name)
            if m:
                found.append((m.group(1), path))
        found.sort(key=lambda x: x[0], reverse=True)
        return found

    def _read_letter(self, day: str, path: Path) -> dict:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        return {
            "date": day,
            "path": str(path),
            "body": post.content.strip(),
            "metadata": dict(post.metadata),
        }

    def latest_dream(self) -> dict | None:
        files = self._letter_files()
        return self._read_letter(*files[0]) if files else None

    def dreams(self, *, limit: int = 8) -> list[dict]:
        out: list[dict] = []
        for day, path in self._letter_files()[:limit]:
            try:
                post = frontmatter.loads(path.read_text(encoding="utf-8"))
                excerpt = _preview(post.content, limit=_PREVIEW_CHARS)
            except Exception:  # noqa: BLE001 — a malformed letter shouldn't break the index
                excerpt = "(could not read letter)"
            out.append({"date": day, "path": str(path), "excerpt": excerpt})
        return out

    def read_dream(self, day: str) -> dict | None:
        for d, path in self._letter_files():
            if d == day:
                return self._read_letter(d, path)
        return None

    # -- write-back (gated on allow_write_back) ---------------------------

    def endorse(self, feedback_event_id: int, rank: int) -> dict:
        """Reinforce the graph path behind candidate #``rank`` of a past recall.

        This is the ``daemon select`` logic factored out of ``cli.py`` so both
        the explicit ``mydaemon_endorse`` tool and the provider's implicit
        soft-reinforcement in ``sync_turn`` go through one place.
        """

        if not self.write_enabled:
            return {"ok": False, "reason": "write-back disabled (hermes.allow_write_back=false)"}
        event = self.feedback.get(feedback_event_id)
        if event is None:
            return {"ok": False, "reason": f"no feedback event {feedback_event_id}"}
        ranked = event.retrieval_summary.get("ranked") or []
        if not ranked:
            return {"ok": False, "reason": "event has no ranked candidates"}
        if rank < 1 or rank > len(ranked):
            return {"ok": False, "reason": f"rank {rank} out of range (1..{len(ranked)})"}

        picked = ranked[rank - 1]
        seed_note = picked.get("seed_note_path") or picked.get("note_path")
        selected_note = picked.get("note_path")

        with self._write_lock:
            result = apply_selection(
                self.graph, seed_note_path=seed_note, selected_note_path=selected_note,
            )
            self.graph.save()
            self.feedback.attach_signal(
                feedback_event_id,
                "candidate_selected",
                selected_rank=rank,
                selected_chunk_id=picked.get("chunk_id"),
                selected_note_path=selected_note,
            )

        return {
            "ok": True,
            "selected_note_path": selected_note,
            "path": result.path,
            "edges_reinforced": result.edges_reinforced,
            "total_delta": result.total_delta,
        }

    def remember(
        self,
        text: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        source: str = "hermes",
        confirmed: bool = False,
        session_id: str | None = None,
        captured_at: datetime | None = None,
    ) -> dict:
        """Capture ``text`` as a provenance-stamped note in the capture folder,
        then incrementally ingest it so it's recallable in the same session.

        Captures land ``status: unconfirmed`` by default (capture-everything,
        promote later — PLAN-HERMES §7) unless ``confirmed`` or
        ``capture_requires_confirmation=false``.
        """

        if not self.write_enabled:
            return {"ok": False, "reason": "write-back disabled (hermes.allow_write_back=false)"}

        moment = captured_at or datetime.now(UTC)
        status = (
            "confirmed"
            if (confirmed or not self.s.hermes.capture_requires_confirmation)
            else "unconfirmed"
        )

        post = frontmatter.Post(
            text.strip() + "\n",
            source=source,
            captured_at=moment.isoformat(),
            status=status,
        )
        if title:
            post["title"] = title
        if session_id:
            post["session_id"] = session_id
        if tags:
            post["tags"] = list(tags)

        rel = f"{self.s.hermes.capture_folder}/{moment.date().isoformat()}/{_slugify(title or text)}.md"
        path = self._dedupe(self._safe_vault_path(rel))
        write_atomic(path, frontmatter.dumps(post) + "\n")

        note = parse_note(path, self.s.vault.path)
        with self._write_lock:
            n_chunks = ingest_note(
                self.s, self.embedder, self.vector_store, self.graph, note,
                sparse_embedder=self.sparse_embedder,
            )

        return {
            "ok": True,
            "path": str(path),
            "rel_path": note.relative_path,
            "status": status,
            "chunks": n_chunks,
        }

    # -- containment helpers ----------------------------------------------

    def _safe_vault_path(self, rel: str) -> Path:
        """Resolve ``rel`` inside the vault, refusing escapes and the Agent folder."""
        root = self.s.vault.path.expanduser().resolve()
        candidate = (root / rel).resolve()
        try:
            parts = candidate.relative_to(root).parts
        except ValueError as exc:
            raise ValueError(f"capture path escapes vault: {candidate}") from exc
        if parts and parts[0].lower() == self.s.agent.folder_name.lower():
            raise ValueError(f"capture target inside {self.s.agent.folder_name}/ (daemon-owned)")
        return candidate

    @staticmethod
    def _dedupe(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix, parent = path.stem, path.suffix, path.parent
        for n in range(2, 1000):
            alt = parent / f"{stem}-{n}{suffix}"
            if not alt.exists():
                return alt
        return parent / f"{stem}-{int(datetime.now(UTC).timestamp())}{suffix}"


def build_core(settings: Settings, *, load_graph: bool = True) -> DaemonCore:
    """Wire the real stores (embedder, Qdrant, graph, feedback, LLM) into a
    :class:`DaemonCore`. The embedder/Qdrant are touched lazily on first use —
    so this is cheap enough to call from the provider's ``initialize`` but must
    NOT be called from ``is_available`` (which Hermes requires to stay network-free).
    """

    embedder = Embedder(
        settings.embeddings.model,
        batch_size=settings.embeddings.batch_size,
        device=settings.embeddings.device,
        cache_folder=settings.embeddings.cache_folder,
    )
    sparse_embedder = (
        SparseEmbedder(
            model_name=settings.embeddings.sparse_model,
            cache_folder=settings.embeddings.cache_folder,
        )
        if settings.embeddings.hybrid
        else None
    )
    vector_store = VectorStore(
        url=settings.vector_store.qdrant.url,
        collection=settings.vector_store.qdrant.collection,
        dim=embedder.dimension,
        hybrid=settings.embeddings.hybrid,
    )
    graph_store = GraphStore(path=settings.graph.path)
    if load_graph:
        graph_store.load()
    feedback_store = FeedbackStore(db_path=settings.feedback.db_path)
    llm_client = LLMClient(settings.llm, api_key=settings.anthropic_api_key)

    return DaemonCore(
        settings,
        embedder=embedder,
        sparse_embedder=sparse_embedder,
        vector_store=vector_store,
        graph_store=graph_store,
        feedback_store=feedback_store,
        llm_client=llm_client,
    )
