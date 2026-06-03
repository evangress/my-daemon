# PLAN-HERMES.md — Integrating My Daemon as the Memory & Dream Layer for Hermes

> **One line:** Hermes *acts*; My Daemon *remembers and dreams*. This plan wires
> the existing graph-augmented vector store + overnight consolidation loop into
> **Hermes** (Nous Research's open-source agent — MIT) so Hermes draws on your
> memory to answer and writes back what it learns.

This is a design + milestone plan, not yet code. It is grounded both in the
modules that already exist here (`QueryEngine`, `retrieval.weights.apply_selection`,
`FeedbackStore`, `pipeline.agent_observe.run_observe`, `vault/writer.py`) **and**
in Hermes's actual integration interfaces, which I read from its repo
(`NousResearch/hermes-agent`) — see §1.

---

## 0. Decisions locked in (from the planning Q&A)

| Question | Decision |
|---|---|
| What is Hermes? | An **open-source agent app** (Nous Research `hermes-agent`, MIT) |
| Transport | **MCP server** — *but see §1: investigation found a more native path* |
| Access level | **Read + write-back** |
| Dream delivery | Dreams **injected + queryable** |

The four decisions stand. What changed after reading Hermes's source is *how
best to deliver them* — see the next section.

---

## 1. Investigation findings — how Hermes actually consumes memory

I investigated the original open question ("does Hermes consume MCP
resources/prompts, or tools only?") directly against
`github.com/NousResearch/hermes-agent` and its docs. Two findings reshape the plan:

### 1a. MCP: tools, resources, and prompts — but resources are **not** auto-injected
Hermes **is** an MCP client and consumes **tools, resources, and prompts**. At
startup it "discovers MCP servers… and registers their tools into the normal
tool registry," and for resources/prompts it adds wrapper tools
(`list_resources`, `read_resource`, `list_prompts`, `get_prompt`). Crucially: it
**does not act on resource/prompt change notifications** and there is **no
resource subscription** — so an MCP `dream://latest` resource is *only* reachable
when the agent **chooses to call `read_resource`**. MCP gives us deliberate,
agent-driven retrieval, not ambient memory. Config lives in
`~/.hermes/config.yaml` under `mcp_servers:` (stdio `command`/`args`/`env` or
HTTP `url`/`headers`).

### 1b. The better fit: Hermes's native **memory-provider plugin** interface
Hermes ships a first-class **memory provider** plugin system (8 built-ins:
Mem0, Honcho, Hindsight, Holographic, RetainDB, OpenViking, ByteRover,
Supermemory). When a provider is active, Hermes automatically:

- **injects a provider block into the system prompt** (volatile tier, at session start),
- **prefetches relevant memories before every turn**,
- **syncs each conversation turn back** to the provider after the response,
- **extracts memories on session end**, and
- **mirrors built-in memory writes** to the provider.

This is *exactly* what My Daemon is. The interface is a Python ABC,
`MemoryProvider` (`agent/memory_provider.py`), and the hooks map almost
one-to-one onto our pipeline:

| Hermes hook | My Daemon mapping | Satisfies decision |
|---|---|---|
| `prefetch(query, *, session_id="")` — before each turn | `QueryEngine.ask(query, synthesize=False)` → grounded, cited context block | **read** (ambient, automatic) |
| `system_prompt_block()` — session-start, cached | latest observer **letter (the dream)** + identity framing | **dream injected** (automatic) |
| `sync_turn(user, assistant, *, session_id="")` — after each turn, **must be non-blocking** | conversation capture + `FeedbackStore.log` | **write-back** (automatic) |
| `get_tool_schemas()` / `handle_tool_call()` | explicit tools: `mydaemon_recall`, `mydaemon_endorse`, `mydaemon_dream`, `mydaemon_remember` | **read + write-back + dream queryable** (deliberate) |
| `on_memory_write(action, target, content)` | mirror Hermes's durable MEMORY.md/USER.md facts into the vault | **write-back** |
| `on_session_end(messages)` | final capture/flush of the session | **write-back** |

So the memory-provider path delivers **all four** locked decisions *more
natively* than MCP — ambient (no agent decision needed), automatic dream
injection at session start, and turn-by-turn capture.

### 1c. Recommendation: a hybrid, **provider-primary**
Build **one core service layer** and expose it through **two faces**:

1. **Primary — a Hermes memory-provider plugin** (`plugins/memory/my-daemon/`).
   Native, ambient, automatic. This is how Hermes *should* talk to My Daemon.
2. **Secondary / optional — the MCP server** (the originally-chosen transport).
   Keeps My Daemon usable by *any* MCP client (Claude Desktop, other agents) and
   by non-Hermes setups. Defer it unless/until a non-Hermes consumer is wanted.

> **Open decision for Evan (see §15):** confirm provider-primary, or keep MCP as
> the primary surface. Everything below is written so the shared core is built
> once regardless; only the milestone ordering changes.

---

## 2. Architecture — two faces, one core

```
        ┌──────────────────────────────────────────────────────────┐
        │  Hermes (Nous Research, MIT)  —  the FRONT END             │
        │  CLI / Telegram / Discord / Slack …  · its own LLM + loop  │
        └───▲───────────────────────────────────────▲───────────────┘
            │ memory-provider plugin (PRIMARY)       │ MCP client (OPTIONAL)
            │  prefetch / system_prompt_block /       │  tools + read_resource
            │  sync_turn / tools                      │
   ┌────────┴────────────────┐              ┌─────────┴───────────────┐
   │ src/my_daemon/hermes/   │              │ src/my_daemon/mcp/      │
   │ provider.py             │              │ server.py (FastMCP)     │
   │ (subclasses Hermes ABC) │              │                         │
   └───────────┬─────────────┘              └───────────┬─────────────┘
               │                                         │
               └──────────────┬──────────────────────────┘
                              ▼
            ┌──────────────────────────────────────────┐
            │  src/my_daemon/integration/core.py  (NEW) │
            │  recall() · endorse() · remember() ·      │
            │  latest_dream() · dreams() · neighbors()  │
            │  — the ONE adapter over the pipeline —    │
            └───┬───────────────┬───────────────┬───────┘
                │               │               │
        ┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼────────┐
        │ QueryEngine  │ │ apply_       │ │ run_observe  │
        │ .ask()       │ │ selection    │ │ (the dream)  │
        └──────────────┘ └──────────────┘ └──────────────┘
              over → Qdrant · networkx graph · SQLite feedback · Obsidian vault
```

The `daemon` CLI stays the operations surface (`ingest`, `snapshot`,
`consolidate`, `status`). The NiceGUI chat (`daemon chat`) becomes an optional
standalone/debug UI — Hermes is the day-to-day front end.

---

## 3. The core service layer (`src/my_daemon/integration/core.py`)

Both faces are thin; all real work lives here, behind plain Python functions
that take already-built stores. This is what we test hardest.

| Function | Wraps | Returns |
|---|---|---|
| `recall(query, *, top_k=8, synthesize=False)` | `QueryEngine.ask` | `{feedback_event_id, answer?, candidates:[{rank, chunk_id, note_path, heading_path, score, preview, seed_note_path}]}` |
| `recall_block(query, *, budget_chars)` | `recall` + formatter | a markdown **context block** (cited by note path), size-capped — for `prefetch()`/`system_prompt_block()` |
| `endorse(feedback_event_id, rank)` | `apply_selection` + `FeedbackStore.attach_signal` | edges reinforced, Δweight (the existing `daemon select` logic, factored out of `cli.py`) |
| `remember(text, *, title=None, tags=[], source, confirmed=False)` | `vault/writer.write_atomic` + single-note `ingest` | path written, indexed |
| `latest_dream()` / `dreams(limit)` / `read_dream(date)` | `<vault>/Agent/observer-*.md` + rolling `observer.md` | letter body / index / one letter |
| `neighbors(note_path, depth=1)` | `GraphStore` | adjacent notes/tags + edge weights |
| `status()` | `GraphStore.stats` + `VectorStore.count` | counts |

`recall` returns **`feedback_event_id`** (from `QueryEngine.ask` →
`FeedbackStore.log`) and per-candidate **`seed_note_path`** (from
`build_retrieval_summary`) precisely because the write-back loop needs them.

---

## 4. Division of labor — retrieval, not a second brain

**`recall` defaults to retrieval-only (no LLM synthesis).** Hermes has its own
model; it wants *grounded, cited context*, not an answer composed for it.

- `prefetch()` and `recall` return ranked chunks + note paths; Hermes reasons.
- Keeps recall **fast and free** (no Anthropic call daemon-side) — important when
  an agent fans out retrievals every turn.
- Clean seam: My Daemon owns memory quality; Hermes owns reasoning/voice.
- `synthesize=True` remains available (NiceGUI, or any client wanting a one-shot
  grounded answer) — just not the agent default. Mirrors the existing
  `QueryEngine.ask(query, synthesize=...)`.

---

## 5. Path A (PRIMARY) — the Hermes memory-provider plugin

### 5.1 Packaging
- Implementation lives **in this repo** (Apache-2.0):
  `src/my_daemon/hermes/provider.py` → `class MyDaemonProvider(MemoryProvider)`,
  importing Hermes's ABC **lazily** so `my_daemon` never hard-depends on Hermes.
- A thin shim is dropped into Hermes's tree at `plugins/memory/my-daemon/`:
  - `__init__.py` → `from my_daemon.hermes import MyDaemonProvider` +
    `def register(ctx): ctx.register_memory_provider(MyDaemonProvider())`
  - `plugin.yaml` (name, version, `hooks:` list), `README.md`, optional `cli.py`.
- Requirement: `pip install my-daemon` into Hermes's venv (documented).
- Activation: `memory: { provider: my-daemon }` in `~/.hermes/config.yaml`;
  provider settings (path to *our* `config.yaml` / vault) in
  `$HERMES_HOME/my-daemon.json` via `get_config_schema()` + `save_config()`.
  Storage paths use the `hermes_home` kwarg (profile isolation, per Hermes rules).

### 5.2 Hook implementations (against the real ABC)
- `name` → `"my-daemon"`; `is_available()` → vault path + config resolve, **no
  network** (so it's safe to probe at init).
- `initialize(session_id, **kwargs)` → build embedder + stores once
  (`_build_*` helpers), stash `hermes_home`, `session_id`.
- `prefetch(query, *, session_id="")` → `core.recall_block(query, budget_chars=…)`
  — ambient associative recall before every turn. (Also logs a `FeedbackEvent`
  and caches its id for soft reinforcement in `sync_turn`.)
- `system_prompt_block()` → `core.latest_dream()` excerpt + a one-paragraph
  identity framing → **automatic dream injection** at session start.
- `sync_turn(user, assistant, *, session_id="")` → **non-blocking daemon thread**
  (per Hermes's threading contract): capture the turn via `core.remember(...)`
  when it clears a salience bar, and attach a soft `candidate_selected`-style
  signal for any prefetched note the assistant actually leaned on.
- `get_tool_schemas()` / `handle_tool_call()` → deliberate tools:
  `mydaemon_recall` (deeper/explicit query), `mydaemon_endorse`
  (explicit reinforcement), `mydaemon_remember` (force-capture a fact),
  `mydaemon_dream` (read a past letter), `mydaemon_neighbors`.
- `on_memory_write(action, target, content)` → mirror Hermes's durable
  MEMORY.md/USER.md facts into the vault as `source: hermes-memory` notes.
- `on_session_end(messages)` → final flush/capture; `shutdown()` → close stores.

### 5.3 Implicit feedback under ambient prefetch
With automatic prefetch there's no human "click," so the signal comes from the
turn: log each `prefetch` as a `FeedbackEvent`, then in `sync_turn` reinforce the
graph path to any prefetched note the assistant cited/used (soft, positive-only —
matching the project's current "no negative decrements yet" stance). The explicit
`mydaemon_endorse` tool remains for when Hermes wants to be deliberate.

---

## 6. Path B (OPTIONAL) — the MCP server, for portability

Unchanged in spirit from the first draft; **deferred** unless a non-Hermes
client is wanted. New `src/my_daemon/mcp/` on the official **`mcp` SDK**
(`FastMCP`), launched via `daemon mcp serve [--transport stdio|http]`
(lazy-import `mcp`, like `daemon chat` lazy-imports `nicegui`).

- **Tools** (all over `core`): `recall` (synthesize defaults false), `search`,
  `read_note`, `neighbors`, `endorse`*, `remember`*, `latest_dream`,
  `list_dreams`, `read_dream`, `status`. (`*` gated by write-back flag.)
- **Resources:** `dream://latest`, `dream://{date}`, `daemon://status`,
  `daemon://identity`. ⚠️ Per §1a these reach Hermes only via an agent
  `read_resource` call — *not* auto-injected — which is exactly why Path A is the
  primary surface for ambient memory + dream injection.
- Hermes-side config (if used): `~/.hermes/config.yaml` →
  ```yaml
  mcp_servers:
    my-daemon:
      command: "daemon"
      args: ["mcp", "serve", "--transport", "stdio"]
      env: { MY_DAEMON_CONFIG: "/home/evan/dev/my-daemon/config.yaml" }
  ```

---

## 7. Memory write-back / capture design

Honors the vision's *"all user chat history will be stored and connected with
RAG results that are confirmed by the user"* and reuses `vault/writer.py`:

- **Where:** `<vault>/Conversations/hermes/<YYYY-MM-DD>/<slug>.md` (configurable)
  — a *normal* (ingest-visible) folder, not the daemon-owned `Agent/`.
- **Provenance frontmatter:** `source: hermes`, `captured_at`, `session_id`,
  and `status: confirmed|unconfirmed`.
- **Atomic + contained:** `write_atomic` (tempfile + `os.replace`); path resolved
  inside `vault_root` (the existing `is_writable` discipline).
- **Indexing:** `remember` incrementally ingests **that one note** so it's
  recallable in-session. Full `ingest --full` and nightly `consolidate` stay
  operator/scheduled (respects "read + write-back, not full control" and the
  project's "keep consolidate manual until it earns trust" stance).
- **Confirmation:** `confirmed=False` captures land `status: unconfirmed` and can
  be down-weighted/filtered until promoted — aggressive capture never pollutes
  high-confidence memory (see §15 Q2).

---

## 8. The dream routine in this topology

`daemon consolidate` (`run_observe`) is unchanged — snapshot → analyze →
observer letter → rolling index → gentle decay → `<vault>/Agent/observer-<date>.md`.
Delivery:

1. **Cadence** — keep `consolidate` operator/scheduled (cron / systemd timer /
   Task Scheduler). Hermes does **not** trigger it (that would be "full control").
2. **Injected** — `system_prompt_block()` puts last night's letter in front of
   Hermes every session, automatically (Path A). MCP's resource (Path B) needs an
   agent `read_resource` call.
3. **Queryable** — letters are ingested like any note (reachable via `prefetch`/
   `recall`) plus explicit `mydaemon_dream` / `read_dream`.

The observer letter's second-person voice ("Letter from your daemon…") is
well-suited to being spoken back through Hermes.

---

## 9. Configuration additions

New `HermesConfig` in `config.py` (+ both example YAMLs), gating like `AgentConfig`:

```python
class HermesConfig(BaseModel):
    # Memory-provider (Path A)
    provider_enabled: bool = False
    capture_folder: str = "Conversations/hermes"
    capture_requires_confirmation: bool = True
    prefetch_budget_chars: int = 4000
    dream_block_budget_chars: int = 3000
    # Shared retrieval defaults handed to the agent
    recall_synthesize_default: bool = False
    recall_top_k: int = 8
    allow_write_back: bool = False          # gates endorse + remember (both faces)
    # MCP server (Path B) — optional
    mcp_enabled: bool = False
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8077
    mcp_auth_token_env: str = "MY_DAEMON_MCP_TOKEN"   # http bearer, env-only
```

HTTP bearer token comes from the environment, never yaml (same rule as
`ANTHROPIC_API_KEY`); the `.env` holding it is already in `.gitignore`
(CLAUDE.md rule 1).

---

## 10. Dependencies & license compliance (CLAUDE.md rule 10)

- **Hermes** (`NousResearch/hermes-agent`): **MIT** → Apache-2.0 compatible. Our
  provider subclasses its ABC at runtime; we ship Apache-2.0 and do not
  redistribute Hermes. ✅
- **`mcp` SDK** (Path B only): **MIT** → Apache-2.0 compatible; Python ≥3.10 (we're
  `>=3.11,<3.15`). Add to `[project.dependencies]`, **lazy-imported** in
  `daemon mcp serve`. ✅
- Path A adds **no** new runtime dependency to `my_daemon` itself (Hermes's ABC is
  imported lazily, only inside Hermes's process).
- Action: add `mcp` (and a note re: the Hermes MIT host dependency) to
  `debug/license-compliance.{md,json}` when implemented.

---

## 11. Security & safety

- **Defaults off:** `provider_enabled`, `mcp_enabled`, `allow_write_back` all
  default `false`. Read-only for weeks before opting into write-back (mirrors
  `agent.enabled`).
- **No network probing** in `is_available()` (Hermes contract) and **non-blocking
  `sync_turn`** (daemon thread) so Hermes's loop never stalls on us.
- **MCP HTTP** binds `127.0.0.1`, requires the env bearer token, never `0.0.0.0`.
- **Capture/read containment** via `vault/writer.py` (`is_writable`, atomic,
  provenance) and vault-root resolution for `read_note`.
- **Key isolation:** Anthropic key stays daemon-side; Hermes never sees it.
- **Off-device note (cloud caution):** Hermes warns providers to document what
  `messages` leaves the device. My Daemon is **local-only** (Qdrant, SQLite,
  filesystem) — capture writes stay on disk; nothing is sent to a third party
  except the user's own configured Anthropic calls. State that plainly in the
  provider README.

---

## 12. Milestones (hybrid, provider-primary)

### H1 — Core service layer + provider read path *(MVP)*
`src/my_daemon/integration/core.py` (recall/recall_block/latest_dream/status/
neighbors). `src/my_daemon/hermes/provider.py` implementing `name`,
`is_available`, `initialize`, `prefetch`, `system_prompt_block`,
`get_tool_schemas`/`handle_tool_call` (read tools). Shim `plugins/memory/my-daemon/`.
`HermesConfig` (read fields). **Tests:** `prefetch` returns a cited, budget-capped
block; `system_prompt_block` returns the newest letter from a fixture `Agent/`;
provider routes a `mydaemon_recall` tool call; `is_available` makes no network call.

### H2 — Provider write-back
`core.endorse` (factor `daemon select` internals out of `cli.py`) and
`core.remember`. Provider `sync_turn` (non-blocking capture + soft reinforcement),
`on_memory_write`, `on_session_end`. Gate behind `allow_write_back`. **Tests:**
`sync_turn` writes a provenance-stamped capture and the note becomes recallable;
soft endorse reinforces the seed→note path (Δweight>0); write hooks no-op when
the flag is off; `sync_turn` returns immediately (thread, not blocking).

### H3 — Dream continuity + deliberate tools
Identity framing in `system_prompt_block`; `mydaemon_dream`/`mydaemon_endorse`/
`mydaemon_remember` tools; document + script nightly `consolidate` cadence.
**Tests:** dream block assembles (and degrades gracefully with no letters yet);
explicit endorse path; capture confirmation policy.

### H4 — MCP server *(optional portability)*
`src/my_daemon/mcp/server.py` (FastMCP) over the same `core`; `daemon mcp serve`;
resources + write-tool gating; `daemon mcp doctor`. **Tests:** in-process MCP
client asserts `recall` shape incl. `feedback_event_id`; `read_note` rejects path
escape; stdio `initialize` + `tools/list` round-trip.

### H5 — Packaging, docs, UX
`docs-source/integrations/hermes.md` (+ mkdocs nav): provider install (pip into
Hermes venv + `memory.provider` config), the MCP alternative, the
recall-not-synthesize rationale, the local-only/off-device statement. Update
`README.md` + `PROJECT_MANAGEMENT.md`. Decide NiceGUI's fate (keep as standalone
debug UI — recommended). Submit the plugin upstream to Hermes's plugin registry
if desired.

---

## 13. Hermes-side setup (illustrative)

**Path A (recommended):**
```bash
# in Hermes's environment
pip install my-daemon
# copy/symlink the shim into Hermes:  plugins/memory/my-daemon/
hermes memory setup            # prompts via get_config_schema() → points at your vault/config
```
```yaml
# ~/.hermes/config.yaml
memory:
  provider: my-daemon
```

**Path B (optional, MCP):** the `mcp_servers:` block shown in §6.

---

## 14. Open questions for Evan

1. **Primary surface.** Confirm **provider-primary hybrid** (recommended:
   ambient + automatic dream injection + turn capture), or keep **MCP-primary**
   (deliberate, portable, but no ambient/auto-injection)? This sets the milestone
   ordering above. *(The original Q&A picked MCP before we knew the provider
   interface existed — hence re-asking.)*
2. **Capture confirmation policy.** `remember`/`sync_turn` default to
   *unconfirmed* (capture-everything, promote later) or only confirmed captures?
   Sets whether unconfirmed memories are recallable or quarantined.
3. **Identity/“purpose” seed.** Draft a short "who you are / why this memory
   exists" note in your voice for `system_prompt_block()` / `daemon://identity`?
   (Picks up the 2026-05-16 AI-suggestion in `PROJECT_MANAGEMENT.md`.)
4. **Single Hermes or many clients?** Add a `client_id`/`session_id` to
   captures + feedback now so the dream can later distinguish what *Hermes*
   reinforced from other sources — cheap now, awkward to retrofit.
5. **NiceGUI:** keep as standalone debug UI (recommended) or retire once Hermes
   is the front end?

---

## 15. Out of scope (deliberately, for now)

- **Agent-triggered consolidation / full re-ingest** — that's "full control";
  dreaming stays operator/scheduled until the loop earns trust.
- **A custom Context Engine plugin** (Hermes's other provider-plugin type, which
  replaces the context compressor) — possible future, not needed for memory.
- **Multi-tenant / remote network exposure** — localhost-first by design.
- **Replacing My Daemon's retrieval with Hermes's own** — My Daemon owns memory;
  Hermes owns acting. Keep the seam.

---

### Appendix — Hermes interface facts (verified from `NousResearch/hermes-agent`, June 2026)
- Memory-provider ABC: `agent/memory_provider.py`; plugins in `plugins/memory/<name>/`
  (`__init__.py` with `register(ctx)`, `plugin.yaml`, `README.md`, optional `cli.py`).
- Hooks: `name`, `is_available` (no network), `initialize(session_id, **kwargs[hermes_home])`,
  `get_tool_schemas`, `handle_tool_call`, `get_config_schema`, `save_config`; optional
  `system_prompt_block`, `prefetch`, `queue_prefetch`, `sync_turn` (non-blocking),
  `on_session_end`, `on_pre_compress`, `on_memory_write`, `shutdown`. **One** external
  provider active at a time.
- System prompt tiers: `stable` (SOUL.md, tools, skills) → `context` (.hermes.md/AGENTS.md/
  CLAUDE.md) → `volatile` (MEMORY.md, USER.md, **memory-provider block**, timestamps).
- MCP: client-side, tools auto-registered; resources/prompts via wrapper tools only; **no**
  resource subscription / change-notification action. Config: `~/.hermes/config.yaml` →
  `mcp_servers:`. Hermes license: **MIT**.
```
