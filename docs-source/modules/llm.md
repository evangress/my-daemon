# `llm/` — Anthropic client, synthesis prompt, agent prompts

Source: `src/my_daemon/llm/`.

## `llm.client.LLMClient`

Thin wrapper around `anthropic.Anthropic()` that holds the `LLMConfig` and an
optional pre-built client. Lazy: the SDK isn't imported until first use.

```python
LLMClient(config: LLMConfig, api_key: str | None)
  .synthesize(query, chunks) -> str
  .synthesize_stream(query, chunks) -> Iterator[str]
```

Both methods build the same kwargs (model, max_tokens, system prompt, user
message). `temperature` is only included if non-null — reasoning-capable
models like Opus 4.7 reject the field, so leaving `temperature: null` in
config makes the client compatible across the whole Claude 4 family.

### Streaming

`synthesize_stream` uses `client.messages.stream(...)` and yields each text
delta as it arrives. The NiceGUI chat consumes this and feeds the bytes into
a markdown label so the response appears progressively.

### API key resolution

The key comes from `Settings.anthropic_api_key`, which `load_settings()`
populates from `os.environ["ANTHROPIC_API_KEY"]` after loading `.env`.
If the key is missing at first call, the client raises a clear error
asking the user to set it.

## `llm.prompts`

Two functions and one system prompt.

### `SYSTEM_PROMPT`

The "you are the user's daemon" framing, plus formatting rules: direct
answer in 1–3 sentences, supporting points each tagged with their source,
surface conflicts rather than paper over them, never invent.

### `build_context_block(chunks) -> str`

Renders the ranked chunks as a numbered list of:

```
[1] note_path › heading › subheading  (score=X.XXX, vector=Y.YYY)
<chunk text>
```

Provenance is `vector=...` for seeds and `graph_distance=...` for expanded
chunks, so the prompt makes the chunk's origin legible to the model.

### `build_user_message(query, chunks) -> str`

Composes the question, the context block, and a one-line instruction to
answer using only the excerpts and to cite each claim.

The whole template is intentionally boring for v0.1. Tuning belongs *after*
retrieval quality is right — premature prompt cleverness is a way to paper
over bad retrieval.

## `llm.agents` — Background-agent prompts

The three writeback jobs share `llm.agents`, which provides:

### Shared helpers

- `_model_for(client, override)` — picks the model: explicit override →
  `LLMConfig.batch_model` → `LLMConfig.model`. Default routes the writeback
  jobs to Haiku 4.5 (~15× cheaper than Opus).
- `_call(client, system, user, model, max_tokens)` — one-shot, non-streaming
  call. Agents always need the full response to parse.
- `_coerce_json(text)` — tolerant JSON extractor: plain body, fenced
  ` ```json ` block, or the first balanced `{...}` substring.

### Extract — `extract_note_observations(client, note)`

System prompt: "you are a quiet, careful reader of one note." Asks for a
JSON object with `summary`, `key_points`, `themes`, `feelings`, `topics`,
`open_questions`. Returns an `AgentNotesPayload` dataclass.

Companion: `render_agent_notes_body(payload, model_used)` formats the
payload into the markdown body that lives between the sentinel markers.

### Link — `propose_links(client, source, candidates)`

System prompt: "review candidate wikilinks for an Obsidian note." For each
candidate target note (passed as `(Note, short_summary)`), decide whether a
wikilink would feel natural and what verbatim anchor text to use. Returns a
list of `LinkSuggestion(target, anchor, confidence, reason)`.

The pipeline uses this as a *second opinion* on the suggestions written to
`Agent/link-suggestions-*.md` — it never overrides the apply gate.

### Reflect — `update_memory(client, theme, prior_memory, recent_notes, recent_chats)`

System prompt: "you are maintaining a memory file about the user — one
theme of who they are." Critical rule: *treat any edits the user has made to
the prior memory as ground truth.* The model receives the prior memory body,
recent notes (each excerpted to ~1200 chars), and recent chat events, then
returns the new markdown body. The pipeline prepends the YAML frontmatter
itself.
