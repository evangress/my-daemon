# Hermes integration

> **One line:** Hermes *acts*; My Daemon *remembers and dreams*.

My Daemon can serve as the **memory and dream layer** for
[Hermes](https://github.com/NousResearch/hermes-agent) (Nous Research's
open-source agent, MIT-licensed). Hermes becomes the day-to-day front end — CLI,
Telegram, Discord, Slack, its own model and loop — while My Daemon supplies
grounded recall from your Obsidian vault and injects the daemon's nightly
reflections.

The full design lives in
[`PLAN-HERMES.md`](https://github.com/evangress/my-daemon/blob/master/PLAN-HERMES.md).
This page is the practical guide.

## Two faces, one core

Everything routes through a single adapter, `my_daemon.integration.core.DaemonCore`
(`recall` · `endorse` · `remember` · `latest_dream` · `neighbors` · `status`),
exposed through two faces:

| Face | Module | Status |
|---|---|---|
| **Memory-provider plugin** (primary) | `my_daemon.hermes.provider` | shipped (H1–H3) |
| **MCP server** (optional portability) | `my_daemon.mcp` | deferred (H4) |

The provider plugin is the native, ambient path: Hermes injects, prefetches, and
syncs automatically. The MCP server is kept on the roadmap for non-Hermes clients
(Claude Desktop, other agents) but isn't needed to run Hermes.

## What the provider does

Once active, Hermes automatically:

- **injects** the daemon's latest observer letter (the "dream") + an identity
  framing into the system prompt at session start (`system_prompt_block`);
- **prefetches** grounded, cited memory before every turn (`prefetch` →
  `DaemonCore.recall_block`);
- **syncs each turn back** (`sync_turn`, non-blocking): captures salient turns
  and softly reinforces the graph path to any recalled note the reply leaned on;
- exposes deliberate tools: `mydaemon_recall`, `mydaemon_dream`,
  `mydaemon_neighbors`, and — only when write-back is enabled —
  `mydaemon_endorse` and `mydaemon_remember`.

### Recall is retrieval-only

`recall` defaults to **no LLM synthesis**. Hermes has its own model and wants
*grounded, cited context*, not an answer composed for it. This keeps recall fast
and free (no daemon-side Anthropic call when an agent fans out retrievals every
turn) and keeps the seam clean: **My Daemon owns memory quality; Hermes owns
reasoning and voice.** `synthesize=True` remains available for the NiceGUI chat
or any client wanting a one-shot grounded answer.

The Anthropic connection stays in My Daemon for its own sub-agent needs — the
observer letter (Opus) and the extract/link/reflect batch jobs (Haiku). Hermes
never sees the API key.

## Install

```bash
# in Hermes's environment
pip install my-daemon
# copy or symlink the shim from this repo into Hermes's tree:
ln -s /path/to/my-daemon/plugins/memory/my-daemon  $HERMES_HOME/plugins/memory/my-daemon
hermes memory setup            # points the provider at your vault / config
```

```yaml
# ~/.hermes/config.yaml
memory:
  provider: my-daemon
```

Point the provider at your My Daemon config via the `hermes memory setup` prompt
(`config_path`) or the `MY_DAEMON_CONFIG` environment variable.

## Configure (My Daemon side)

A new `hermes:` block in `config.yaml` gates everything — **all off by default**:

```yaml
hermes:
  provider_enabled: false        # kill-switch: is_available() returns False until true
  capture_folder: Conversations/hermes
  capture_requires_confirmation: true
  capture_min_chars: 120
  prefetch_budget_chars: 4000
  dream_block_budget_chars: 3000
  identity: ""                   # blank → built-in identity framing
  recall_synthesize_default: false
  recall_top_k: 8
  allow_write_back: false        # gates endorse + remember (both faces)
  # MCP server (Path B, optional) — deferred
  mcp_enabled: false
  mcp_transport: stdio
  mcp_host: 127.0.0.1
  mcp_port: 8077
  mcp_auth_token_env: MY_DAEMON_MCP_TOKEN
```

Verify readiness without touching the network:

```bash
daemon hermes doctor
```

It reports whether `is_available()` would let Hermes activate the provider, the
capture/write-back settings, and how many observer letters (dreams) exist to inject.

## Write-back, gently

Run **read-only for a while** before opting into write-back (mirrors
`agent.enabled`). With `allow_write_back: false`, `endorse`, `remember`, and the
`sync_turn` capture are all no-ops, and the two write tools aren't even offered
to the agent.

When you flip it on:

- **Captures** land in `Conversations/hermes/<date>/<slug>.md` — a *normal*,
  ingest-visible folder (not the daemon-owned `Agent/`) — with provenance
  frontmatter (`source`, `captured_at`, `session_id`, `status`). They default to
  `status: unconfirmed` (capture-everything, promote later) unless explicitly
  confirmed.
- **Soft reinforcement** is positive-only: when the assistant's reply leans on a
  prefetched note, the seed→note graph path is reinforced — the same mechanism
  as `daemon select`, just implicit.

## The dream in this topology

`daemon consolidate` is unchanged — it stays operator/scheduled (cron, systemd
timer, Task Scheduler). Hermes does **not** trigger it (that would be "full
control"). The letters reach Hermes three ways:

1. **Injected** automatically at session start via `system_prompt_block`.
2. **Queryable** like any note through `prefetch` / `recall`.
3. **Deliberate** via the `mydaemon_dream` tool (latest, or a specific date).

## Safety & privacy

- **Local-only.** Memory lives in Qdrant, SQLite, and your vault, all on disk.
  Captures stay on disk; nothing leaves the device except your own configured
  Anthropic calls, which Hermes never sees.
- **Defaults off.** `provider_enabled` and `allow_write_back` both default false.
- **Non-blocking.** `sync_turn` runs on a daemon thread; `is_available` makes no
  network call.
- **Contained writes.** Captures go through the same atomic, vault-root-contained
  writer as the rest of My Daemon, and never touch `Agent/`.

## License

Hermes is **MIT** → Apache-2.0 compatible. The provider subclasses Hermes's
`MemoryProvider` ABC **lazily at runtime** (resolving to `object` when Hermes
isn't importable), so `my_daemon` adds **no** new runtime dependency for Path A
and does not redistribute Hermes. The optional MCP SDK (Path B) is MIT and would
be lazy-imported only when `daemon mcp serve` lands.
