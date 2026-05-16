# My Daemon — App Theme

How the daemon's chat surface looks, why it looks that way, and how to extend
it without drifting. Scoped to **the app** (the chat UI in
`src/my_daemon/gui/app.py`); the marketing site has its own brand reference at
`../my-daemon-astro-website/BRAND.md`, and **that file wins** when this one
drifts.

---

## 1. The brand thread

The daemon is a triple pun — Pullman's external soul-companion, Socrates'
inner advisory voice, and a Unix background process. The surface should feel
like all three: warm, philosophical, technical. A whisper, not a shout. Dark,
but paper-warm rather than generic SaaS dark mode.

**The trichromatic story, applied to chat:**

```
gold   →  the human's question      (seed / spark / italic emphasis)
violet →  the daemon itself          (wordmark, primary CTA)
cyan   →  the synthesis speaking back (daemon's reply)
```

> A question (gold) calls a daemon (violet) which speaks back (cyan).

This story is the visual spine of the chat: user bubbles carry a gold stripe,
daemon bubbles carry a cyan stripe, and the wordmark + Send pill carry violet.
Don't recolor bubbles for any other reason.

---

## 2. Palette

All values are OKLCH. The chat is dark-mode-only for now.

| Role             | OKLCH                          | Where it shows up                                     |
|------------------|--------------------------------|-------------------------------------------------------|
| Background       | `oklch(14% 0.025 282)`         | `<body>` — deep midnight indigo                       |
| Body text (ink)  | `oklch(92% 0.018 90)`          | Default text — warm-tinted near-white                 |
| Muted ink        | `oklch(92% 0.018 90 / 0.70)`   | Eyebrows, epigraph, marginalia                        |
| Hairline rule    | `oklch(60% 0.05 285 / 0.20)`   | Input underline at rest, dividers                     |
| **Violet**       | `oklch(72% 0.20 305)`          | Wordmark dot, primary CTA, Quasar `primary`           |
| **Cyan**         | `oklch(78% 0.14 195)`          | Daemon-bubble stripe + border tint; `<a>` underlines  |
| **Gold**         | `oklch(82% 0.155 75)`          | User-bubble stripe; italic `<em>`; input focus; caret |

Behind the content live three corner-anchored radial gradients (violet, cyan,
indigo at very low alpha) — that's what makes the dark theme read as
paper-warmth rather than terminal black. Don't remove them.

The bubbles are **low-chroma tinted surfaces with a 2px hue stripe on the
speaker's side**, not saturated fills:

```css
/* user (right) */  border-right: 2px solid oklch(82% 0.155 75 / 0.55);
/* daemon (left) */ border-left:  2px solid oklch(78% 0.14 195 / 0.55);
```

Saturated bubble fills look like a chat product; the stripe reads like a
margin mark in a notebook.

---

## 3. Typography

Three Google-hosted families. Same families as the marketing site so the chat
feels continuous; same display swap so first paint isn't blocked.

| Family             | CSS variable     | Job in the chat                                          |
|--------------------|------------------|----------------------------------------------------------|
| **Newsreader**     | `--font-body`    | Default body — daemon's replies, input value             |
| **Fraunces** (italic dominant) | `--font-display` | Wordmark, epigraph, user-bubble text, markdown headings inside daemon replies |
| **JetBrains Mono** | `--font-mono`    | Eyebrow vault chip, CTA pill micro-caps, inline `<code>` |

**Forbidden:** Inter, Roboto, Arial, system-ui as body or display. (Mono is
fine for mono.) If you need a fourth family for a future panel, audition
EB Garamond or iA Quattro first — the rule is *character before convenience*.

Default tone is italic Fraunces at low opacity — that's the brand's whisper,
and the chat epigraph uses it verbatim: *"Ask, and I will walk the vault with
you."* with a gold colored period.

---

## 4. Components

### Wordmark — `.wordmark`
Violet dot + small-caps Fraunces, weight 500, letter-spacing `0.28em`. The dot
has a 12px violet outer glow. Used **once per surface**, top-left. On hover
(not implemented in the chat — we don't navigate), the text would shift to
`text-primary` per the marketing site.

### Eyebrow — `.eyebrow`
JetBrains Mono, uppercase, `0.68rem`, letter-spacing `0.32em`, opacity `0.70`.
Use for the vault chip, status labels, future metadata strips. Never for body
content.

### Epigraph — `.epigraph`
Italic Fraunces at `1.02rem` with a slight gold-period accent. Reserve for a
single line above the chat column, not as a recurring divider.

### Bubbles
- **User bubble:** italic Fraunces, gold stripe on the right edge, right-aligned in the row.
- **Daemon bubble:** Newsreader, cyan stripe on the left edge, left-aligned in the row.
- Both: 8px radius, `0.30` shadow, max-width `70ch`, line-height `1.65–1.70`.
- Inside the daemon bubble, Markdown is parsed live (`ui.markdown`). `<em>` is recolored gold, headings get Fraunces, `<code>` gets a faint mono chip, blockquotes get a gold left rule.

### Input — `.input-rule`
Borderless Quasar input wrapped in a `<div>` whose **only** chrome is a 1px
bottom hairline (`--rule`). On focus the hairline becomes gold; the caret is
gold. Placeholder is italic at `0.40` opacity. **Never** switch to a boxed
`outlined` input — boxed inputs read as "form field," not "instrument."

### Send button — `.cta-pill`
Rounded-full violet pill (Quasar `unelevated color=primary`), label in
JetBrains Mono uppercase at `0.7rem` with `0.24em` tracking. The brand reserves
one such CTA per viewport — Send is the chat's. Don't add a second pill.

---

## 5. Where the theme lives

All of the above is concentrated in one place:

`src/my_daemon/gui/app.py` — the top of `_mount_ui` defines the OKLCH constants
and emits the `<style>` block via `ui.add_head_html`. The Google Fonts `<link>`
sits in the same block so the head is built atomically.

If you add a new NiceGUI surface (a settings drawer, a sources panel, a
candidate picker), reuse the existing CSS classes rather than re-declaring
colors inline. New classes go in the same `<style>` block, keyed off the same
`--violet / --cyan / --gold / --ink / --rule` custom properties.

---

## 6. Editorial don'ts

Stolen from BRAND.md § 11; the ones that apply to the chat:

- ❌ Inter, Roboto, Arial, system-ui as a display or body font.
- ❌ A glowing violet orb labelled "AI" with no further context (the wordmark dot is the *only* place violet glows).
- ❌ Gradient text on a white background.
- ❌ Heavy emoji.
- ❌ "Sign up free" / "Get started" / 🚀 copy.
- ❌ Saturated chat-bubble fills.
- ❌ Multiple `glow-ring` / pill CTAs per viewport.

If a piece of UI starts to feel like one of these, stop and ask whether
there's a more *daemonic* form for it — a footnote, a marginalia, a hairline.

---

## 7. Source of truth

`../my-daemon-astro-website/BRAND.md` is canonical for the brand as a whole
(voice, palette, motifs, motion). This file is the **app-side digest** — the
subset that applies to the chat surface, plus the concrete CSS class names
used in `gui/app.py`. When the two diverge, update this file to match the
marketing brand, not the other way around.
