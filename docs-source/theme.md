# Theme

How the daemon's surfaces look, and why. This page summarises the app-side
brand reference at [`THEME.md`](https://github.com/evangress/my-daemon/blob/master/THEME.md)
in the repo root. When the two diverge, `THEME.md` (and ultimately
`my-daemon-astro-website/BRAND.md`) wins — update this page, not the other
way around.

## The brand thread

The daemon is a triple pun: Pullman's external soul-companion, Socrates'
inner advisory voice, a Unix background process. The surface should feel
like all three — warm, philosophical, technical. A whisper, not a shout.
Dark, but paper-warm rather than generic SaaS dark mode.

## Trichromatic story

```
gold   →  the human's question      (seed / spark / italic emphasis)
violet →  the daemon itself          (wordmark, primary CTA)
cyan   →  the synthesis speaking back (daemon's reply)
```

> A question (gold) calls a daemon (violet) which speaks back (cyan).

This story is the visual spine of the chat. User bubbles carry a gold
stripe; daemon bubbles carry a cyan stripe; the wordmark and Send pill
carry violet. Don't recolor bubbles for any other reason.

## Palette (OKLCH)

| Role | OKLCH | Where it shows up |
|---|---|---|
| Background | `oklch(14% 0.025 282)` | `<body>` — deep midnight indigo |
| Body text (ink) | `oklch(92% 0.018 90)` | Default text — warm-tinted near-white |
| Muted ink | `oklch(92% 0.018 90 / 0.70)` | Eyebrows, epigraph, marginalia |
| Hairline rule | `oklch(60% 0.05 285 / 0.20)` | Input underline at rest, dividers |
| **Violet** | `oklch(72% 0.20 305)` | Wordmark dot, primary CTA |
| **Cyan** | `oklch(78% 0.14 195)` | Daemon-bubble stripe, links |
| **Gold** | `oklch(82% 0.155 75)` | User-bubble stripe, italic emphasis, focus |

Behind the content live three corner-anchored radial gradients (violet,
cyan, indigo at very low alpha) — that's what makes the dark theme read as
paper-warmth rather than terminal black.

## Typography

| Family | CSS variable | Job |
|---|---|---|
| Newsreader | `--font-body` | Daemon's replies, input value |
| Fraunces (italic dominant) | `--font-display` | Wordmark, epigraph, user-bubble text, daemon-bubble headings |
| JetBrains Mono | `--font-mono` | Eyebrow vault chip, CTA pill micro-caps, inline `<code>` |

Default tone is italic Fraunces at low opacity — the brand's whisper. The
chat epigraph reads "*Ask, and I will walk the vault with you.*" with a
gold-colored period.

## Components

- **Wordmark** — violet dot + Fraunces small-caps, used once per surface,
  top-left.
- **Eyebrow** — JetBrains Mono uppercase, `0.68rem`, `0.32em` tracking,
  `0.70` opacity. The vault chip and any status labels.
- **Epigraph** — italic Fraunces, `1.02rem`, with a gold-period accent.
  Single line above the chat column; not used as a recurring divider.
- **User bubble** — italic Fraunces, gold right-edge stripe, right-aligned.
- **Daemon bubble** — Newsreader body, cyan left-edge stripe, left-aligned.
  Markdown is parsed live: `<em>` recolored gold, headings get Fraunces,
  `<code>` gets a mono chip, blockquotes get a gold left rule.
- **Input** — borderless Quasar input wrapped in a 1px bottom hairline.
  Focus turns the hairline gold; caret is gold. Never use a boxed
  `outlined` input.
- **Send pill** — rounded-full violet button, JetBrains Mono micro-caps
  label. One CTA per viewport. Don't add a second pill.

## Editorial don'ts

- ❌ Inter, Roboto, Arial, system-ui as a display or body font.
- ❌ A glowing violet orb labelled "AI" with no further context.
- ❌ Gradient text on a white background.
- ❌ Heavy emoji.
- ❌ "Sign up free" / "Get started" / 🚀 copy.
- ❌ Saturated chat-bubble fills.
- ❌ Multiple `glow-ring` / pill CTAs per viewport.

If a piece of UI starts to feel like one of these, stop and ask whether
there's a more *daemonic* form for it — a footnote, a marginalia, a
hairline.

## Where the theme lives in code

- **Chat (NiceGUI)** — `src/my_daemon/gui/app.py`. The top of `_mount_ui`
  defines OKLCH constants and emits a single `<style>` block via
  `ui.add_head_html`, with the Google Fonts `<link>` in the same block so
  the head is built atomically.
- **Setup window (Tkinter)** — `src/my_daemon/gui/setup.py`. Hex
  approximations of the OKLCH values (Tkinter doesn't speak OKLCH), applied
  via `ttk.Style` and the `clam` theme. The visual story is the same: violet
  primary CTA, gold focus / success accent, deep indigo background.

When you add a new surface, reuse the existing CSS classes rather than
redeclaring colours inline.
