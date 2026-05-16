"""Synthesis prompt template. Intentionally boring for v0.1 — tune later."""

from __future__ import annotations

from my_daemon.models import RetrievedChunk

SYSTEM_PROMPT = """You are the user's "daemon" — a memory companion with read-only access to their personal Obsidian vault.

Your job is to answer their question using ONLY the provided excerpts from their own notes. Cite every claim by note path and heading path. If the provided context does not contain the answer, say so plainly — do not invent details, dates, or names. The user wants their actual memory back, not a plausible-sounding reconstruction.

Format:
- Begin with a direct answer in 1–3 sentences.
- Follow with brief supporting points, each tagged with its source like (note_path › heading › subheading).
- If multiple excerpts conflict, surface the conflict instead of papering over it.
"""


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, rc in enumerate(chunks, start=1):
        heading = " › ".join(rc.chunk.heading_path) if rc.chunk.heading_path else "(no heading)"
        score = rc.combined_score
        provenance = (
            f"vector={rc.vector_score:.3f}" if rc.vector_score is not None else f"graph_distance={rc.graph_distance}"
        )
        parts.append(
            f"[{i}] {rc.chunk.note_path} › {heading}  (score={score:.3f}, {provenance})\n{rc.chunk.text.strip()}"
        )
    return "\n\n---\n\n".join(parts)


def build_user_message(query: str, chunks: list[RetrievedChunk]) -> str:
    context = build_context_block(chunks) if chunks else "(no excerpts retrieved)"
    return f"""Question: {query}

Excerpts from the vault:

{context}

Answer the question using only these excerpts. Cite each supporting claim by note path and heading.
"""
