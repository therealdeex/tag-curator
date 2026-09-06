#!/usr/bin/env bash
# Deploy the stash-tag-curator working tree to the production Stash host.
#
# Production layout (verified 2026-08-27):
#   host:        shahram@192.168.8.40  (tailscale: docker-personal)
#   stash:       systemd stash.service, dir /mnt/stash-virtiofs/stashapp
#   plugin dir:  /mnt/stash-virtiofs/stashapp/plugins/stash-tag-curator
#                (symlink -> /home/shahram/dev/tag-ops/stash-tag-curator)
#   data dir:    /mnt/stash-virtiofs/stashapp/stash-tag-curator-data
#
# The deploy replaces the checkout contents ONLY. The data dir (rules,
# SQLite state, rollback journal) lives outside it and is never touched.
# After syncing, reload plugins in Stash (Settings > Plugins, or the
# reloadPlugins mutation) to pick up the new code.
#
# SAFETY: assets/*.json are RUNTIME-WRITTEN snapshot mirrors (written by
# whatever instance last ran the plugin from this tree). They must never
# be deployed -- a dev instance's mirrors would shadow production data in
# the dashboard until the next production task regenerates them.

set -euo pipefail

REMOTE=${1:-shahram@192.168.8.40}
DEST=/home/shahram/dev/tag-ops/stash-tag-curator
HERE="$(cd "$(dirname "$0")/.." && pwd)"

cd "$HERE"

echo ">> removing local transient snapshot mirrors (never deploy these)"
rm -f assets/*.json

echo ">> backing up remote checkout"
ssh "$REMOTE" "cd /home/shahram/dev/tag-ops && \
  tar czf stash-tag-curator-pre-deploy-backup-\$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
  --exclude='__pycache__' stash-tag-curator/"

echo ">> rsyncing working tree -> $REMOTE:$DEST"
rsync -az --delete \
  --exclude=".venv" --exclude="__pycache__" --exclude=".pytest_cache" \
  --exclude=".hypothesis" --exclude=".git" --exclude=".playwright-mcp" \
  --exclude=".opencode" --exclude="dist" --exclude="dist-test" \
  --exclude="*.log" --exclude="*.pyc" --exclude="assets/*.json" \
  ./ "$REMOTE:$DEST/"

echo ">> remote sanity"
ssh "$REMOTE" "cd $DEST && grep '^version' stash-tag-curator.yml \
  && python3 -m compileall -q curator/ && echo PY-COMPILE-OK"

# assets/*.json on the REMOTE are production's own runtime mirrors (Stash
# writes them into the checkout it runs from). They are excluded from the
# rsync in both directions; the local tree is cleaned above so we never
# ship dev mirrors over production data.

echo ">> done. Reload plugins in Stash, then dispatch the read-only"
echo ">> 'Refresh Data' task once to regenerate production snapshots."
