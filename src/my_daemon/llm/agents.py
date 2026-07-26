# SPDX-License-Identifier: Apache-2.0
"""Prompt builders + thin LLMClient wrappers for the three background-agent jobs.

Each function takes a configured ``LLMClient`` (the same one the chat uses) and a
model name to call against — typically ``LLMConfig.batch_model`` (Haiku) for cost
containment, with a fallback to ``LLMConfig.model`` when None.

The agents only ever produce structured payloads or full file bodies. They
never decide whether to write — that's the pipeline's job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from my_daemon.llm.client import LLMClient
from my_daemon.models import (
    FeedbackEvent,
    Note,
    StructuralReport,
    WeightEvolutionReport,
)


@dataclass
class AgentNotesPayload:
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    feelings: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


@dataclass
class LetterTheme:
    """One emergent theme, flattened for the observer letter's prompt.

    The dream phase mints these (cluster → reconcile → name) and the letter is
    the only place the user meets them in prose, so the letter has to run
    *after* clustering and receive them.
    """

    label: str
    summary: str = ""
    is_new: bool = False
    query_count: int = 0
    note_titles: list[str] = field(default_factory=list)


@dataclass
class LinkSuggestion:
    target: str       # target note title
    anchor: str       # exact substring of the source body to wrap as [[target|anchor]]
    confidence: float # 0..1 LLM-rated
    reason: str = ""


# ----------------------------------------------------------------- shared helpers


def _model_for(client: LLMClient, override: str | None = None) -> str:
    """Pick the model the agent should use: explicit override > batch_model > main model."""
    if override:
        return override
    return client.config.batch_model or client.config.model


def _call(client: LLMClient, *, system: str, user: str, model: str, max_tokens: int = 1500) -> str:
    """One-shot, non-streaming Anthropic call — agents always need the full response."""
    anth = client._client_()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if client.config.temperature is not None:
        kwargs["temperature"] = client.config.temperature
    message = anth.messages.create(**kwargs)
    out: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "\n".join(out).strip()


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _coerce_json(text: str) -> dict | list:
    """Tolerantly extract JSON from a model response: full body, or fenced block."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: scan for the first balanced { ... } object.
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"could not parse JSON from model response: {text[:200]!r}")


# ----------------------------------------------------------------- extract


_EXTRACT_SYSTEM = """You are the user's daemon — a quiet, careful reader of their personal notes.

You are reading ONE note and extracting observations that will be written back into the note itself under an "Agent Notes" section. The user will see what you write, so address them in second person ("you wrote...", "you mention...").

Rules:
- Do not invent. If something is not in the note, do not include it.
- Quote sparingly — paraphrase in the user's register.
- Be specific. "You are exploring identity" is too vague; "You are weighing whether to stay in Austin or move back to the coast" is right.
- Feelings: only the ones the note itself expresses, named carefully. Skip if absent.
- Open questions: things the note leaves unanswered; the user may want to revisit them.

Return ONLY a JSON object with keys:
  summary (string, 2-3 sentences),
  key_points (list of 3-7 short strings),
  themes (list of 1-5 short noun phrases),
  feelings (list of 0-5 single words; omit if the note isn't affective),
  topics (list of 1-8 short noun phrases),
  open_questions (list of 0-3 short questions; omit if none).
"""


def extract_note_observations(
    client: LLMClient,
    note: Note,
    *,
    model: str | None = None,
    max_chars: int = 8000,
) -> AgentNotesPayload:
    """Ask the LLM to extract structured observations from a single note."""
    body = note.body[:max_chars]
    user = (
        f"Note title: {note.title}\n"
        f"Note path: {note.relative_path}\n\n"
        f"--- body ---\n{body}\n--- end body ---\n\n"
        "Respond with ONLY the JSON object described in the system prompt."
    )
    raw = _call(
        client, system=_EXTRACT_SYSTEM, user=user, model=_model_for(client, model),
        max_tokens=1200,
    )
    data = _coerce_json(raw)
    if not isinstance(data, dict):
        raise ValueError(f"extract returned non-object: {data!r}")
    return AgentNotesPayload(
        summary=str(data.get("summary", "")).strip(),
        key_points=[str(x) for x in (data.get("key_points") or [])],
        themes=[str(x) for x in (data.get("themes") or [])],
        feelings=[str(x) for x in (data.get("feelings") or [])],
        topics=[str(x) for x in (data.get("topics") or [])],
        open_questions=[str(x) for x in (data.get("open_questions") or [])],
    )


def render_agent_notes_body(payload: AgentNotesPayload, *, model_used: str) -> str:
    """Render an AgentNotesPayload into the markdown body that lives between sentinel markers."""
    today = datetime.now().strftime("%Y-%m-%d")
    parts: list[str] = [
        f"_Last updated {today} by your daemon (model: {model_used})._",
        "",
    ]
    if payload.summary:
        parts += [f"**Summary.** {payload.summary}", ""]
    if payload.key_points:
        parts.append("**Key points.**")
        parts += [f"- {p}" for p in payload.key_points]
        parts.append("")
    if payload.themes:
        parts.append(f"**Themes.** {', '.join(f'_{t}_' for t in payload.themes)}")
    if payload.feelings:
        parts.append(f"**Feelings.** {', '.join(f'_{f}_' for f in payload.feelings)}")
    if payload.topics:
        parts.append(f"**Topics.** {', '.join(f'_{t}_' for t in payload.topics)}")
    if payload.open_questions:
        parts += ["", "**Open questions.**"]
        parts += [f"- {q}" for q in payload.open_questions]
    return "\n".join(parts).rstrip() + "\n"


# ----------------------------------------------------------------- link


_LINK_SYSTEM = """You are reviewing candidate wikilinks for an Obsidian note.

You will be given:
- The source note's title and body.
- A list of candidate target notes (title + 1-2 sentence summary).

For each candidate, decide if a wikilink from the source to that target would feel natural to the user — i.e. the source mentions the target's subject in a way a reader would expect to be clickable. If yes, choose the EXACT substring in the source body to wrap as the anchor text.

Rules:
- The anchor MUST be a verbatim case-insensitive substring of the source body.
- Reject vague matches. The target's title or a close paraphrase must actually appear in the source.
- Do not invent target titles; only use the ones given.
- Confidence 0..1: 1.0 = unambiguous (the source literally names the target), 0.5 = thematically related, < 0.5 = skip.

Return ONLY a JSON array; each element is an object with keys: target, anchor, confidence, reason (one short sentence).
"""


def propose_links(
    client: LLMClient,
    source: Note,
    candidates: list[tuple[Note, str]],  # (candidate_note, short_summary)
    *,
    model: str | None = None,
    body_max_chars: int = 6000,
) -> list[LinkSuggestion]:
    """LLM second-opinion on candidate wikilinks. Returns suggestions with a confidence score."""
    if not candidates:
        return []
    cand_lines = [
        f"- {i + 1}. {n.title!r}: {summary[:160].strip()}"
        for i, (n, summary) in enumerate(candidates)
    ]
    user = (
        f"Source note title: {source.title}\n"
        f"Source note path: {source.relative_path}\n\n"
        f"--- source body ---\n{source.body[:body_max_chars]}\n--- end source body ---\n\n"
        "Candidate target notes:\n"
        + "\n".join(cand_lines)
        + "\n\nRespond with ONLY the JSON array described in the system prompt."
    )
    raw = _call(
        client, system=_LINK_SYSTEM, user=user, model=_model_for(client, model),
        max_tokens=1200,
    )
    data = _coerce_json(raw)
    if not isinstance(data, list):
        raise ValueError(f"propose_links returned non-array: {data!r}")
    out: list[LinkSuggestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        target = str(item.get("target", "")).strip()
        anchor = str(item.get("anchor", "")).strip()
        if not target or not anchor:
            continue
        out.append(
            LinkSuggestion(
                target=target,
                anchor=anchor,
                confidence=confidence,
                reason=str(item.get("reason", "")).strip(),
            )
        )
    return out


# ----------------------------------------------------------------- reflect


_REFLECT_SYSTEM = """You are maintaining a memory file about the user — one theme of who they are, in their own vault.

You will be given:
- The current contents of the memory file (your prior take on this theme — may be empty).
- A batch of recent notes the user has written.
- A batch of recent chat events between the user and the daemon.

Rewrite the memory file. Rules:
- Preserve load-bearing observations from the prior memory unless new evidence contradicts them.
- Treat any edits the user has made to the prior memory as ground truth.
- Never invent biographical claims — no names, dates, places, or relationships that do not appear in the sources.
- Write in second person, as if the user will read it.
- Note explicit uncertainty when evidence is thin ("based on a single recent note...").
- Keep the file scannable: short paragraphs, the occasional bullet list.
- Begin with a one-paragraph distillation, then sectioned observations.

Return ONLY the new markdown body (no frontmatter, no fences). The pipeline will prepend the YAML provenance header itself.
"""


def update_memory(
    client: LLMClient,
    theme: str,
    prior_memory: str,
    recent_notes: list[Note],
    recent_chats: list[FeedbackEvent],
    *,
    model: str | None = None,
    note_excerpt_chars: int = 1200,
) -> str:
    """Produce the new body for ``Agent/memory-<theme>.md`` based on prior memory + recent signal."""
    note_blocks: list[str] = []
    for n in recent_notes:
        excerpt = n.body[:note_excerpt_chars].strip()
        note_blocks.append(f"### {n.title}  ({n.relative_path})\n{excerpt}")
    recent_chats = answered_only(recent_chats)
    chat_blocks: list[str] = []
    for ev in recent_chats:
        chat_blocks.append(
            f"- Q: {ev.query.strip()[:300]}\n  A: {(ev.answer or '').strip()[:400]}"
        )

    user_parts: list[str] = [
        f"Theme to maintain: **{theme}**",
        "",
        "--- prior memory file (may be empty) ---",
        prior_memory.strip() or "(empty)",
        "--- end prior memory ---",
        "",
        f"--- {len(recent_notes)} recent notes ---",
        "\n\n".join(note_blocks) or "(none)",
        "--- end recent notes ---",
        "",
        f"--- {len(recent_chats)} recent chats ---",
        "\n".join(chat_blocks) or "(none)",
        "--- end recent chats ---",
        "",
        "Rewrite the memory file body now. Markdown only.",
    ]
    return _call(
        client, system=_REFLECT_SYSTEM, user="\n".join(user_parts),
        model=_model_for(client, model), max_tokens=2000,
    )


# ----------------------------------------------------------------- observer (M4)


_OBSERVER_SYSTEM = """You are the user's daemon, writing a short letter to them about what their second brain has been doing while they were away.

You will be given:
- A structural snapshot of the user's vault graph: communities the algorithm found, notes that bridge those communities, "load-bearing" links whose removal would disconnect parts of the graph, orphan notes (poorly connected), dangling wikilink targets (notes-they-keep-meaning-to-write), and the warmest edges (the ones that have been most reinforced by the user's recent picks).
- A weight-evolution preview: which edges *would* shift if the recent feedback were replayed against the snapshot.
- A short list of the user's most recent chats with the daemon.
- The **emergent themes**: clusters of the user's own questions that kept landing on the same notes, each already named. Some are new this run; some are recurring, and a recurring theme is a concern the user keeps returning to. A churn number says how settled the themes are overall.
- The bodies of up to a handful of your own prior letters, for continuity.

Write a single-page **second-person markdown letter** that interprets these patterns *semantically*. Treat the structural numbers as evidence, not subject matter — the user already saw the JSON. Your job is to translate the numbers into things like "your daemon-related notes have started pulling Obsidian-tooling notes into the same conversation" or "the journal entries about Austin keep being the bridge between projects and relationships — they are doing structural work in your second brain."

Rules:
- Do not invent. If a community's top tags don't suggest a clear theme, name the uncertainty ("a cluster I can't yet read") instead of pretending.
- Weave the emergent themes into the letter by name — they are the closest thing you have to *what the user has been wondering about*, as opposed to what their files look like. Say plainly which ones are new and which keep coming back. If churn is high (above ~0.5) or a theme appeared only this run, say the reading is provisional.
- Address the user directly. Warm, but not saccharine. Not a corporate report.
- Name *uncertainty* when evidence is thin: "this is from one week of selections, so take it as a hunch, not a verdict."
- Continuity: when a prior letter said something that's still true (or no longer true), acknowledge it explicitly. Avoid restating it verbatim.
- Length: aim for ~400–700 words. A letter, not a memo. Use light section headings if it helps the reader, but it's fine to be one or two flowing paragraphs.
- The user *will* read this in Obsidian, alongside their own notes. Reference notes by their relative path (e.g. `Pullman Daemons.md`) when you want to point at one — but don't link them as `[[wikilinks]]`; the daemon never inserts wikilinks into its own output.
- Sign off with a single line in italics that names which snapshot id and model wrote the letter.

Return ONLY the markdown body (no frontmatter, no fences). The pipeline will prepend the YAML provenance header itself.
"""


def _render_communities(report: StructuralReport, *, max_count: int) -> str:
    if not report.communities:
        return "(no Louvain communities of size > 1 yet)"
    lines: list[str] = []
    for c in report.communities[:max_count]:
        tag_str = ", ".join(f"#{t} ({n})" for t, n in c.top_tags[:3]) or "(no shared tags)"
        member_str = ", ".join(c.members[:8])
        if len(c.members) > 8:
            member_str += f", … (+{len(c.members) - 8} more)"
        lines.append(
            f"- Community {c.community_id} (size {c.size}): {member_str}  — top tags: {tag_str}"
        )
    return "\n".join(lines)


def _render_bridging(report: StructuralReport) -> str:
    if not report.bridging_notes:
        return "(no notes with notable betweenness yet)"
    return "\n".join(
        f"- {b.note_path}  (betweenness {b.betweenness:.4f})"
        for b in report.bridging_notes
    )


def _render_bridges(report: StructuralReport) -> str:
    if not report.bridge_edges:
        return "(no load-bearing note↔note links)"
    return "\n".join(
        f"- {e.src} ↔ {e.dst}  ({e.kind}, weight {e.weight:.2f})"
        for e in report.bridge_edges
    )


def _render_warm(report: StructuralReport) -> str:
    if not report.warm_edges:
        return "(no reinforced edges yet — everything is at baseline weight)"
    return "\n".join(
        f"- {e.src} → {e.dst}  ({e.kind}, weight {e.weight:.2f})"
        for e in report.warm_edges
    )


def _render_dangling(report: StructuralReport) -> str:
    if not report.dangling_targets:
        return "(no dangling wikilink targets)"
    return "\n".join(
        f"- {d.target}  (referenced by {d.incoming_links} note(s))"
        for d in report.dangling_targets
    )


def _render_evolution(evolution: WeightEvolutionReport | None) -> str:
    if evolution is None:
        return "(weight-evolution replay was skipped this run)"
    if evolution.events_replayed == 0:
        return f"(no selection events in the last {evolution.lookback_days} days)"
    lines = [
        f"Events replayed: {evolution.events_replayed} over {evolution.lookback_days} days.",
        f"Events skipped (insufficient info): {evolution.events_skipped}.",
        "Top edge shifts:",
    ]
    for d in evolution.top_edges[:8]:
        lines.append(
            f"  - {d.src} → {d.dst}  ({d.kind}): {d.before:.2f} → {d.after:.2f} "
            f"(Δ {d.delta:+.2f})"
        )
    if evolution.top_notes:
        lines.append("Top notes by aggregate shift:")
        for n in evolution.top_notes[:6]:
            lines.append(
                f"  - {n.note_path}  (Δ {n.total_delta:.2f} across {n.edges_changed} edge(s))"
            )
    return "\n".join(lines)


def answered_only(events: list[FeedbackEvent]) -> list[FeedbackEvent]:
    """Drop feedback rows that carry no answer.

    A row with an empty answer is a *retrieval* record, not a conversation: an
    ambient Hermes prefetch, or a retrieval-only tool call. Rendering them as
    "recent chats" pads the observer's and reflector's prompts with turns that
    never happened, and Hermes generates one per turn.
    """

    return [ev for ev in events if (ev.answer or "").strip()]


def _render_recent_chats(recent: list[FeedbackEvent], *, limit: int = 8) -> str:
    recent = answered_only(recent)
    if not recent:
        return "(no recent chats)"
    chunks: list[str] = []
    for ev in recent[:limit]:
        chunks.append(
            f"- {ev.timestamp.date().isoformat()}  Q: {ev.query.strip()[:200]}\n"
            f"    A: {(ev.answer or '').strip()[:300]}"
        )
    return "\n".join(chunks)


def _render_themes(themes: list[LetterTheme], *, churn: float | None = None) -> str:
    if not themes:
        return "(no emergent themes yet)"
    lines: list[str] = []
    if churn is not None:
        lines.append(
            f"Theme churn: {churn:.2f} (0 = the same themes as last run, "
            "1 = nothing carried over)."
        )
    for t in themes:
        age = "new this run" if t.is_new else "recurring"
        notes = ", ".join(t.note_titles[:6]) or "(no resolvable notes)"
        lines.append(f"- **{t.label}** — {age}, {t.query_count} question(s); notes: {notes}")
        if t.summary:
            lines.append(f"    {t.summary}")
    return "\n".join(lines)


def _render_prior_letters(letters: list[str]) -> str:
    if not letters:
        return "(no prior letters)"
    parts: list[str] = []
    for i, body in enumerate(letters, start=1):
        snippet = body.strip()[:1800]
        parts.append(f"--- prior letter {i} ---\n{snippet}\n--- end prior letter {i} ---")
    return "\n\n".join(parts)


def observer_letter(
    client: LLMClient,
    *,
    structural: StructuralReport,
    evolution: WeightEvolutionReport | None,
    recent_feedback: list[FeedbackEvent],
    prior_letters: list[str],
    snapshot_id: str,
    themes: list[LetterTheme] | None = None,
    theme_churn: float | None = None,
    max_communities: int = 8,
    model: str | None = None,
    max_tokens: int = 2400,
) -> str:
    """Produce the markdown body of the observer letter for a snapshot.

    The pipeline (``pipeline.agent_observe``) handles persistence, snapshot
    creation, rolling index maintenance, and decay. This function only does
    the LLM call and returns the prose.
    """

    user = "\n\n".join(
        [
            f"Snapshot id: **{snapshot_id}**",
            f"Notes: {structural.note_count}, tags: {structural.tag_count}, "
            f"edges: {structural.edge_count}, communities found: {structural.community_count}",
            "--- communities ---\n" + _render_communities(structural, max_count=max_communities),
            "--- bridging notes (sampled betweenness) ---\n" + _render_bridging(structural),
            "--- load-bearing note↔note links ---\n" + _render_bridges(structural),
            "--- warmest edges (recent reinforcement) ---\n" + _render_warm(structural),
            "--- dangling wikilink targets ---\n" + _render_dangling(structural),
            "--- hypothetical weight evolution ---\n" + _render_evolution(evolution),
            "--- recent chats ---\n" + _render_recent_chats(recent_feedback),
            "--- emergent themes (this run's clustering of your questions) ---\n"
            + _render_themes(themes or [], churn=theme_churn),
            "--- prior letters (for continuity) ---\n" + _render_prior_letters(prior_letters),
            "Write the letter now. Markdown body only.",
        ]
    )
    return _call(
        client,
        system=_OBSERVER_SYSTEM,
        user=user,
        model=_model_for(client, model),
        max_tokens=max_tokens,
    )


_THEME_NAMER_SYSTEM = """You name recurring themes in someone's personal knowledge vault.

You are shown a handful of note titles that a cluster of the user's own questions kept landing on, plus a few of those questions verbatim. Name what connects them.

Rules:
- The label is 2-5 words, in the user's register, naming the *concern* — not the mechanism. "Why projects stall" beats "Cluster of project notes".
- The summary is one sentence saying what the user seems to be circling.
- Do not invent detail beyond what the titles and questions support. If they don't cohere, say so in the summary and give a deliberately plain label.
- Never use the words "cluster", "theme", "embedding", or "vector" in the label.

Return exactly two lines:
LABEL: <the label>
SUMMARY: <the sentence>
"""


def name_theme(
    client,  # noqa: ANN001
    *,
    note_titles: list[str],
    representative_queries: list[str],
    model: str | None = None,
) -> tuple[str, str]:
    """Name one emergent theme. Titles only — never chunk bodies.

    Called for *new* clusters only, so steady state is zero or one call a
    night, on the cheap model.
    """

    titles = "\n".join(f"- {t}" for t in note_titles[:12]) or "(none)"
    questions = "\n".join(f"- {q}" for q in representative_queries[:3]) or "(none)"
    user = (
        f"Notes this cluster keeps landing on:\n{titles}\n\n"
        f"Questions from the cluster:\n{questions}"
    )
    text = _call(
        client,
        system=_THEME_NAMER_SYSTEM,
        user=user,
        model=_model_for(client, model),
        max_tokens=200,
    )

    label, summary = "", ""
    for line in (text or "").splitlines():
        if line.upper().startswith("LABEL:"):
            label = line.split(":", 1)[1].strip()
        elif line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
    return label or "unnamed", summary
