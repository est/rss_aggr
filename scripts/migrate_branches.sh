#!/usr/bin/env bash
#
# One-time migration to the ADR-015 branch layout:
#   data      -> runtime state only (state.json, articles.json)
#   gh-pages  -> rendered output only (output/)
#
# By default this script only prints the plan. Pass --apply to rewrite the
# remote branches. Local branches and the working tree are never modified.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/migrate_branches.sh [--apply]

Without --apply: fetch origin and print the migration plan (no remote writes).
With --apply:    back up the old branches and force-push the new snapshots.
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

# Runtime state from the old gh-pages layout.
for f in state.json articles.json; do
  if [ -f "$TMP_DIR/legacy/$f" ]; then
    cp "$TMP_DIR/legacy/$f" "$TMP_DIR/data/$f"
  fi
done
if [ ! -f "$TMP_DIR/data/articles.json" ] && [ -f "$TMP_DIR/legacy/cache.json" ]; then
  cp "$TMP_DIR/legacy/cache.json" "$TMP_DIR/data/articles.json"
  echo "note: migrating legacy cache.json to articles.json"
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
  echo "  2. Create orphan 'data' containing only runtime state:"
  for f in state.json articles.json; do
    [ -f "$TMP_DIR/data/$f" ] && echo "       - $f"
  done
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
  git -C "$TMP_DIR/data" commit -q -m "data: migrate runtime state off gh-pages"
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
