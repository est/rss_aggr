#!/usr/bin/env bash
#
# LEGACY (ADR-015 / ADR-016). Do not run this script after the repository has
# migrated to the permanent Markdown archive architecture (ADR-017).
#
# ADR-017 removes gh-pages entirely and stores the durable archive on the data
# branch. The supported migration path is the first collect run, which restores
# legacy state/output once and uses src.backfill to create archive/ + feeds/.
# For a manual migration use:
#
#   scripts/backfill_archive.sh OLD_ARTICLES_JSON --root . --feeds feeds \
#     --out articles.json
#
# This script remains only for recovering an old ADR-015 checkout. It rewrites
# gh-pages to contain output/ only (legacy behavior); if it creates the data
# branch, it uses src.backfill so the snapshot also contains the Markdown
# archive and feed indexes. Never rewrite gh-pages after ADR-017 migration.
#
# By default this script only prints the plan. --apply is destructive: it
# force-pushes rewritten gh-pages/data branches after creating backup/* refs.
# Verify the printed plan and take a manual backup before using --apply.
# Local branches and the working tree are never modified.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/migrate_branches.sh [--apply]

LEGACY: this script targets the ADR-015 layout and rewrites gh-pages.
ADR-017 no longer uses gh-pages; do not run this after migration.
Use the first collect run plus src.backfill / scripts/backfill_archive.sh
for the supported ADR-017 migration.

Without --apply: fetch origin and print the migration plan (no remote writes).
With --apply:    DESTRUCTIVE. Back up the old branches, then force-push the
                 rewritten data/gh-pages snapshots. Review the plan first and
                 confirm no collect run is active.
EOF
}

APPLY=false
case "${1:-}" in
  "")
    ;;
  --apply)
    APPLY=true
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: run this script inside the repository" >&2
  exit 1
fi

cd "$(git rev-parse --show-toplevel)"

REMOTE_NAME="${REMOTE_NAME:-origin}"
if ! REMOTE_URL="$(git remote get-url "$REMOTE_NAME" 2>/dev/null)"; then
  echo "error: remote '$REMOTE_NAME' is not configured" >&2
  exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_SITE="backup/gh-pages-${TIMESTAMP}"
BACKUP_DATA="backup/data-${TIMESTAMP}"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rss-migrate.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Fetching ${REMOTE_NAME}..."
git fetch --prune "$REMOTE_NAME"

SITE_REF="refs/remotes/${REMOTE_NAME}/gh-pages"
DATA_REF="refs/remotes/${REMOTE_NAME}/data"

if ! SITE_SHA="$(git rev-parse --verify --quiet "${SITE_REF}^{commit}")"; then
  echo "error: ${REMOTE_NAME}/gh-pages does not exist; nothing to migrate" >&2
  exit 1
fi

DATA_SHA=""
DATA_EXISTED=false
if DATA_SHA="$(git rev-parse --verify --quiet "${DATA_REF}^{commit}")"; then
  DATA_EXISTED=true
fi

mkdir -p "$TMP_DIR/legacy" "$TMP_DIR/data" "$TMP_DIR/site"
git archive "$SITE_REF" | tar -x -C "$TMP_DIR/legacy"

# Runtime state from the old gh-pages layout. Legacy article bodies are
# sanitized into archive/ and feeds/ by src.backfill; articles.json itself is
# metadata-only (ADR-017).
if [ -f "$TMP_DIR/legacy/state.json" ]; then
  cp "$TMP_DIR/legacy/state.json" "$TMP_DIR/data/state.json"
fi

LEGACY_ARTICLES=""
if [ -f "$TMP_DIR/legacy/articles.json" ]; then
  LEGACY_ARTICLES="$TMP_DIR/legacy/articles.json"
elif [ -f "$TMP_DIR/legacy/cache.json" ]; then
  LEGACY_ARTICLES="$TMP_DIR/legacy/cache.json"
  echo "note: migrating legacy cache.json to metadata-only articles.json"
fi
if [ -n "$LEGACY_ARTICLES" ]; then
  # ADR-017: sanitize the legacy bodies into the permanent Markdown archive
  # and write metadata-only articles.json with archive offsets.
  python -m src.backfill \
    --articles "$LEGACY_ARTICLES" \
    --root "$TMP_DIR/data" \
    --feeds "$TMP_DIR/data/feeds" \
    --out "$TMP_DIR/data/articles.json"
  echo "note: backfilled legacy article bodies into archive/ and feeds/"
fi

# The new data layout also carries rendered output. Keep historical pages.
if [ -d "$TMP_DIR/legacy/output" ]; then
  cp -R "$TMP_DIR/legacy/output" "$TMP_DIR/data/output"
fi

CREATE_DATA=false
if [ "$DATA_EXISTED" = true ]; then
  echo "note: ${REMOTE_NAME}/data already exists; it will not be overwritten by migration"
elif [ -f "$TMP_DIR/data/state.json" ] || [ -f "$TMP_DIR/data/articles.json" ]; then
  CREATE_DATA=true
else
  echo "warning: no state.json/articles.json found on ${REMOTE_NAME}/gh-pages"
  echo "         and no data branch exists; refusing to rewrite gh-pages without a state copy"
  exit 1
fi

# Rendered output from the old gh-pages layout.
if [ -d "$TMP_DIR/legacy/output" ]; then
  cp -R "$TMP_DIR/legacy/output" "$TMP_DIR/site/output"
else
  echo "warning: no output/ directory found on ${REMOTE_NAME}/gh-pages"
  mkdir -p "$TMP_DIR/site/output"
  touch "$TMP_DIR/site/output/.gitkeep"
fi

echo
echo "Migration plan"
echo "  old gh-pages: ${SITE_SHA}"
if [ "$DATA_EXISTED" = true ]; then
  echo "  old data:     ${DATA_SHA}"
else
  echo "  old data:     (does not exist)"
fi
echo "  backup refs:  ${BACKUP_SITE}"
if [ "$DATA_EXISTED" = true ]; then
  echo "                ${BACKUP_DATA}"
fi
echo
echo "  1. Back up the current remote branches under refs/heads/backup/."
if [ "$CREATE_DATA" = true ]; then
  echo "  2. Create orphan 'data' containing runtime state, archive/ and feeds/:"
  for f in state.json articles.json; do
    [ -f "$TMP_DIR/data/$f" ] && echo "       - $f"
  done
  [ -d "$TMP_DIR/data/archive" ] && echo "       - archive/"
  [ -d "$TMP_DIR/data/feeds" ] && echo "       - feeds/"
else
  echo "  2. Keep the existing 'data' branch unchanged."
fi
echo "  3. Rewrite 'gh-pages' to contain only output/ ($(find "$TMP_DIR/site/output" -type f | wc -l | tr -d ' ') files)."
echo "  4. Force-push the snapshots."
echo

if [ "$APPLY" != true ]; then
  echo "Dry run only. Re-run with --apply to perform the migration."
  echo
  echo "Backups that --apply will create:"
  echo "  git push ${REMOTE_NAME} ${SITE_SHA}:refs/heads/${BACKUP_SITE}"
  if [ "$DATA_EXISTED" = true ]; then
    echo "  git push ${REMOTE_NAME} ${DATA_SHA}:refs/heads/${BACKUP_DATA}"
  fi
  echo
  echo "Rollback after applying:"
  echo "  git push --force ${REMOTE_NAME} ${SITE_SHA}:refs/heads/gh-pages"
  if [ "$DATA_EXISTED" = true ]; then
    echo "  git push --force ${REMOTE_NAME} ${DATA_SHA}:refs/heads/data"
  elif [ "$CREATE_DATA" = true ]; then
    echo "  git push ${REMOTE_NAME} --delete data"
  fi
  exit 0
fi

echo "WARNING: --apply is destructive and only appropriate for the legacy ADR-015 layout."
echo "Creating remote backups..."
git push "$REMOTE_NAME" "${SITE_SHA}:refs/heads/${BACKUP_SITE}"
if [ "$DATA_EXISTED" = true ]; then
  git push "$REMOTE_NAME" "${DATA_SHA}:refs/heads/${BACKUP_DATA}"
fi

git_config() {
  local dir="$1"
  git -C "$dir" config user.name "github-actions[bot]"
  git -C "$dir" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
}

if [ "$CREATE_DATA" = true ]; then
  echo "Creating data snapshot..."
  git -C "$TMP_DIR/data" init -q
  git -C "$TMP_DIR/data" checkout -q --orphan data
  git_config "$TMP_DIR/data"
  git -C "$TMP_DIR/data" add -A
  git -C "$TMP_DIR/data" commit -q -m "data: migrate runtime state and Markdown archive off gh-pages"
  git -C "$TMP_DIR/data" push --force "$REMOTE_URL" "data:refs/heads/data"
fi

# Refuse to delete legacy runtime files from gh-pages unless a data branch
# with runtime state exists (either pre-existing or just pushed above).
if [ "$DATA_EXISTED" = true ]; then
  if ! git cat-file -e "${DATA_REF}:state.json" 2>/dev/null \
      || ! git cat-file -e "${DATA_REF}:articles.json" 2>/dev/null; then
    echo "error: ${REMOTE_NAME}/data exists but is missing state.json or articles.json; refusing to rewrite gh-pages" >&2
    echo "       fix the data branch first (or remove it) before rerunning --apply" >&2
    exit 1
  fi
fi

echo "Creating gh-pages snapshot..."
git -C "$TMP_DIR/site" init -q
git -C "$TMP_DIR/site" checkout -q --orphan gh-pages
git_config "$TMP_DIR/site"
git -C "$TMP_DIR/site" add -A
git -C "$TMP_DIR/site" commit -q -m "site: migrate gh-pages to output-only snapshot"
git -C "$TMP_DIR/site" push --force "$REMOTE_URL" "gh-pages:refs/heads/gh-pages"

echo
echo "Migration complete."
echo
echo "Backups:"
echo "  refs/heads/${BACKUP_SITE} = ${SITE_SHA}"
if [ "$DATA_EXISTED" = true ]; then
  echo "  refs/heads/${BACKUP_DATA} = ${DATA_SHA}"
fi
echo
echo "Rollback:"
echo "  git push --force ${REMOTE_NAME} ${SITE_SHA}:refs/heads/gh-pages"
if [ "$DATA_EXISTED" = true ]; then
  echo "  git push --force ${REMOTE_NAME} ${DATA_SHA}:refs/heads/data"
elif [ "$CREATE_DATA" = true ]; then
  echo "  git push ${REMOTE_NAME} --delete data"
fi
echo
echo "After rollback, also delete the new backup refs if they are no longer needed."
