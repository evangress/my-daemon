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
from my_daemon.models import FeedbackEvent, Note


@dataclass
class AgentNotesPayload:
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    feelings: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


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
