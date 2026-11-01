#!/usr/bin/env bash
#
# Backfill a legacy raw articles.json into the permanent Markdown archive.
#
# This is a thin wrapper around `python -m src.backfill`; it writes the
# metadata-only articles.json (default: ./articles.json) and then validates
# the generated archive. The migration is idempotent: links/guids already in
# feeds/*.jsonl are skipped.
#
# Usage:
#   scripts/backfill_archive.sh OLD_ARTICLES_JSON [src.backfill options]
#
# Examples:
#   scripts/backfill_archive.sh /tmp/legacy-articles.json
#   scripts/backfill_archive.sh /tmp/legacy-articles.json --out articles.json
#   scripts/backfill_archive.sh /tmp/legacy-articles.json --root . --feeds feeds
#
# Environment:
#   OUT    output metadata file (default: articles.json)
#
# The default archive root is ./archive. If you pass --root, validate that
# archive manually with: python -m src.md_archive --validate ROOT/archive

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/backfill_archive.sh OLD_ARTICLES_JSON [options]

Runs:
  python -m src.backfill --articles OLD_ARTICLES_JSON --out articles.json [options]
  python -m src.md_archive --validate archive

Options are passed through to src.backfill:
  --root DIR     project root (archive files go to DIR/archive)
  --feeds DIR    per-feed JSONL directory (default: feeds)
  --out FILE     metadata-only output (default: $OUT or articles.json)

Environment:
  OUT            default output file (default: articles.json)
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ $# -eq 0 ]; then
  usage
  [ $# -eq 0 ] && exit 2
  exit 0
fi

ARTICLES="$1"
shift

if [ ! -f "$ARTICLES" ]; then
  echo "error: legacy articles file not found: $ARTICLES" >&2
  exit 1
fi

OUT="${OUT:-articles.json}"

python -m src.backfill --articles "$ARTICLES" --out "$OUT" "$@"

# Validate the default archive location. A custom --root archive must be
# checked separately (see the note above).
python -m src.md_archive --validate archive
