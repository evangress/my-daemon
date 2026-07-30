# SPDX-License-Identifier: Apache-2.0
"""Synthesis prompt template. Intentionally boring for v0.1 — tune later."""

from __future__ import annotations

from my_daemon.models import Chunk, RetrievedChunk

_RECONCILIATION_RULE = """
When two or more excerpts make competing claims about the same thing, or when the
answer depends on which statement is more recent, reconcile them visibly before
answering. Emit a short block:

── Reconciling dated statements ──
  <claim>  (<date>, <note>)   ← most recent
  <claim>  (<date>, <note>)   superseded

Then give the answer. If no excerpts compete, do not emit the block at all.
"""

_UPDATE_VS_CONTRADICTION_RULE = """
Treat a change of mind and a disagreement differently.

An UPDATE resolves by recency: if the same fact changed over time — a plan, a
preference, a decision — the later-dated statement supersedes the earlier one.
Say what changed and when.

A CONTRADICTION goes back to the user: if two excerpts conflict and recency
cannot settle it (both undated, same date, or a genuine disagreement of fact
rather than a change of mind), present BOTH, say plainly that the notes disagree,
and ask which is correct. Do not choose. Do not average.
"""

_NO_COMPUTATION_RULE = """
Do not compute. Never calculate, derive, or adjust a numeric value across
excerpts. If one note says "I have 2 dogs" and another says "I have a dog named
Rex", do NOT conclude there are 3. Report what the notes say. Arithmetic across
separate notes invents facts that appear in none of them.
"""

_ABSTENTION_RULE = """
A confident "I don't know" is always a correct answer. If the excerpts do not
contain the answer, say so and stop — do not reason toward a plausible answer
from adjacent material. Partial beats invented: "your notes cover the decision
but not the date" is a good answer.
"""

_UNDATED_RULE = """
An excerpt marked (undated) carries no date. Never place it in a sequence and
never treat it as recent. An excerpt whose date ends in ~ is INFERRED from when
the file changed, not stated in the note: usable for ordering, but say it is
approximate if the answer depends on it.
"""

SYSTEM_PROMPT = f"""You are the user's "daemon" — a memory companion with read-only access to their personal Obsidian vault.

Your job is to answer their question using ONLY the provided excerpts from their own notes. Cite every claim by note path and heading path. If the provided context does not contain the answer, say so plainly — do not invent details, dates, or names. The user wants their actual memory back, not a plausible-sounding reconstruction.

Format:
- Begin with a direct answer in 1–3 sentences.
- Follow with brief supporting points, each tagged with its source like (note_path › heading › subheading).
- If multiple excerpts conflict, surface the conflict instead of papering over it.
{_RECONCILIATION_RULE}
{_UPDATE_VS_CONTRADICTION_RULE}
{_NO_COMPUTATION_RULE}
{_ABSTENTION_RULE}
{_UNDATED_RULE}
You may also be shown "Earlier, you asked" — past questions of theirs that drew on the same notes. Use them only to notice a pattern worth naming ("you've circled this three times since March") or to connect the current question to an earlier one. They are the user's own past questions, not evidence: never cite them as sources, and never treat an earlier question as an answer.
"""


def _date_marker(chunk: Chunk) -> str:
    """How a chunk's date is shown to the model.

    `~` means inferred (from mtime) rather than stated. The distinction is not
    decoration: an inferred date is usable for ordering but must not be
    presented to the user as certain, and the system prompt says so.
    """

    if chunk.occurred_at is None:
        return "(undated)"
    stamp = chunk.occurred_at.date().isoformat()
    return f"[{stamp}~]" if chunk.occurred_at_source == "mtime" else f"[{stamp}]"


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, rc in enumerate(chunks, start=1):
        heading = " › ".join(rc.chunk.heading_path) if rc.chunk.heading_path else "(no heading)"
        # Retrieval scores are deliberately absent. They are facts about the
        # machinery, no instruction consumes them, and showing them invites the
        # model to treat our rank order as evidence about the world.
        parts.append(
            f"[{i}] {_date_marker(rc.chunk)} {rc.chunk.note_path} › {heading}\n"
            f"{rc.chunk.text.strip()}"
        )
    return "\n\n---\n\n".join(parts)


def build_memory_block(memories) -> str:  # noqa: ANN001 — avoids an import cycle
    """Past questions that lit up the same notes. Empty when there are none."""

    if not memories:
        return ""
    lines = []
    for m in memories:
        shared = ", ".join(m.shared_notes) or "(no shared notes)"
        lines.append(
            f'- {m.ts.date().isoformat()}: "{m.text}"  '
            f"(same notes: {shared}; similarity {m.score:.2f})"
        )
    return "Earlier, you asked:\n" + "\n".join(lines)


def build_user_message(query: str, chunks: list[RetrievedChunk], memories=None) -> str:  # noqa: ANN001
    context = build_context_block(chunks) if chunks else "(no excerpts retrieved)"
    memory_block = build_memory_block(memories or [])
    if memory_block:
        memory_block = f"\n\n{memory_block}\n"
    return f"""Question: {query}{memory_block}

Excerpts from the vault:

{context}

Answer the question using only these excerpts. Cite each supporting claim by note path and heading.
"""
