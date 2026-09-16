# Panel branding images

Wide brand images shown inside Components-V2 panels (via a `MediaGallery`).

## shuffle_logo.png — Shuffle verify panel logotype

Shown as a banner at the very top of the Shuffle verify panel.

**Max dimensions:** **1246 × 200 px** (the full Components-V2 content width),
transparent PNG. Keep it within this bound — don't exceed it.

- 1246 px is the widest a MediaGallery image renders inside a Container, so a
  1246-px-wide banner fills the panel edge-to-edge at full resolution (Discord
  downscales it on narrower clients).
- 200 px is the max height — keep the *logotype* (wordmark) within it so the
  banner stays a slim header and doesn't dominate the panel. Any height ≤ 200 px
  is fine; the artwork can be shorter with transparent padding.
- Keep it a wide logotype, not a square icon. Transparent background so it sits
  cleanly on the container's surface.

If `shuffle_logo.png` is missing, the panel still posts — just without the banner.

> Note: the small **square** Shuffle logo used on the *Verify* button is a
> separate file — `assets/emojis/shuffle.png` (128 × 128, uploaded as the bot's
> application emoji). See `assets/emojis/README.md`.

## roobet_logo.png — Roobet verify panel logotype

Same rules as `shuffle_logo.png` above: wide transparent PNG, max **1246 x 200 px**.
Shipped at **774 x 200** (the full 2000x517 logotype scaled to the 200 px height cap).

Generated from `Admin-Dashboard/frontend/public/roobet-logo.svg` (the same artwork
the landing page's casino strip uses), so the brand mark stays consistent across
surfaces. Brand yellow is `#EEAF0E` — that is also the panel's accent colour.

> The small **square** Roobet chip mark used on the *Verify* button is a separate
> file — `assets/emojis/roobet.png` (128 x 128). See `assets/emojis/README.md`.
