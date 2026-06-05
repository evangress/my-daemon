<!-- SPDX-License-Identifier: Apache-2.0 -->
# My Daemon — Hermes memory provider

Make [My Daemon](https://github.com/evangress/my-daemon) the **memory & dream
layer** for [Hermes](https://github.com/NousResearch/hermes-agent). Hermes *acts*;
My Daemon *remembers and dreams*.

When this provider is active, Hermes automatically:

- **injects** your daemon's latest observer letter (the "dream") + an identity
  framing into the system prompt at session start;
- **prefetches** grounded, cited memory from your Obsidian vault before every turn;
- **syncs each turn back** — capturing salient turns and softly reinforcing the
  graph path to any recalled note the reply actually used;
- exposes deliberate tools: `mydaemon_recall`, `mydaemon_dream`,
  `mydaemon_neighbors`, and (when write-back is enabled) `mydaemon_endorse` and
  `mydaemon_remember`.

Recall is **retrieval-only by default**: Hermes has its own model and wants
cited context, not an answer composed for it. My Daemon owns memory quality;
Hermes owns reasoning and voice.

## Install

```bash
# in Hermes's environment
pip install my-daemon
# copy or symlink this directory into Hermes:
ln -s /path/to/my-daemon/plugins/memory/my-daemon  $HERMES_HOME/plugins/memory/my-daemon
hermes memory setup            # points the provider at your vault / config
```

```yaml
# ~/.hermes/config.yaml
memory:
  provider: my-daemon
```

Point the provider at your My Daemon config either via the `hermes memory setup`
prompt (`config_path`) or the `MY_DAEMON_CONFIG` environment variable. Verify
with `daemon hermes doctor` on the My Daemon side.

## Safety & privacy

- **Local-only.** Memory lives in Qdrant, SQLite, and your vault — all on disk.
  Conversation captures stay on disk; nothing leaves the device except the
  user's own configured Anthropic calls, which **Hermes never sees** (the API
  key stays daemon-side).
- **Defaults off.** `hermes.provider_enabled` and `hermes.allow_write_back`
  default `false`. Run read-only for a while before opting into write-back.
- **Contained captures.** Captures land in a normal vault folder
  (`Conversations/hermes/` by default), never the daemon-owned `Agent/`, and go
  through the same atomic, vault-root-contained writer the rest of My Daemon uses.
- **Non-blocking.** `sync_turn` runs on a daemon thread so Hermes's loop never
  stalls; `is_available` makes no network call.

See [`PLAN-HERMES.md`](https://github.com/evangress/my-daemon/blob/master/PLAN-HERMES.md)
for the full design.
