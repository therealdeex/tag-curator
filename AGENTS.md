# Agent working rules — stash-tag-curator

## Stash environments: dev vs production (HARD RULE)

- **Never touch production Stash.** Do not browse it, do not take screenshots or
  snapshots of it, do not query its GraphQL, do not restart or reconfigure it.
  This includes anything on `192.168.8.40` (docker-personal VM) or any stash
  instance that is not the local dev one below. Taking snapshots of the
  production environment is against protocol — the owner explicitly forbade
  this (2026-09-28).
- **All UI verification happens against the local dev Stash on this machine:**
  - URL: `http://127.0.0.1:9998` (LAN: `http://192.168.8.123:9998`)
  - Install: `/opt/stash-dev/stash` (Stash v0.31.1), config
    `/opt/stash-dev/config.yml`, DB `/opt/stash-dev/stash-go.sqlite`
  - This plugin is symlinked in: `/opt/stash-dev/plugins/stash-tag-curator`
    → this repo. UI/JS changes are picked up by a browser reload; no restart
    needed.
  - Login credentials + API key: stored owner-approved in
    `/home/shahram/dev/stash-plugins/AGENTS.md` ("Stash dev server access"
    section). Strip credentials before making any repo public.
  - The dev fixture is a small cartoon/TV library — safe to run read-only
    dashboard refreshes against; still confirm before running destructive
    tasks (Update Library) on it.

## Stash v0.31.1 plugin UI conventions (learned the hard way)

- Plugin **pages** live at `/plugins/<id>` (plural) — that is the only path the
  server serves the SPA shell for. `/plugin/<id>` (singular) is the **assets**
  mount and hard-404s as a page URL.
- Register the plural path as the primary route and the singular path as a
  legacy client route (same pattern as stash-justwatch).
- Add nav entries via `api.patch.before("MainNavBar.MenuItems", ...)` ONLY.
  Patching `MainNavBar.UtilityItems` too makes the entry appear twice (see
  stash-reels). The plugin's nav patch also dedupes: it skips appending when a
  curator entry is already present in the menu.
