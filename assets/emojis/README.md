# Panel brand emojis (application emojis)

Square brand logos uploaded as the bot's **application emojis** and shown on panel
buttons in place of unicode emojis.

Required files (square, transparent PNG, **128×128**, ≤ 256 KB each — Discord's
application-emoji size limit):

- `kick.png`    — Kick logo   → combined link panel "Link Kick" button (else 🟢)
- `twitch.png`  — Twitch logo → combined link panel "Link Twitch" button (else 🟣)
- `shuffle.png` — Shuffle logo → Shuffle verify panel "Verify" button (else 🎰)
- `roobet.png`  — Roobet chip mark → Roobet verify panel "Verify" button (else 🦘)

On startup the bot uploads each as an application emoji the first time
(`ensure_link_emojis()` in `combined_link_panel.py` for kick/twitch;
`ensure_shuffle_emoji()` in `shuffle_panel.py` for shuffle, `ensure_roobet_emoji()`
in `roobet_panel.py` for roobet), then reuses them by name on later boots.

These same application emojis are also selectable on **Discord live-alert
buttons** (dashboard → Discord live alerts). A button stores the emoji as an
`app:<name>` token (e.g. `app:kick`); `build_alert_components()` in
`core/stream_notifications.py` resolves the token to the custom emoji by name at
send time (unicode fallback if the upload is missing). The dashboard previews use
copies of these PNGs under `Admin-Dashboard/frontend/src/assets/emojis/` (imported
so Vite resolves them under the app's `/static/dist/` base) — keep them in sync if
you change the art.

If a file is missing or the upload fails, that button falls back to its unicode
emoji and the panel still works — so the bot won't break if these aren't in place
yet.

> The **wide** Shuffle logotype shown at the top of the verify panel is a separate
> image — `assets/branding/shuffle_logo.png`. See `assets/branding/README.md`.
