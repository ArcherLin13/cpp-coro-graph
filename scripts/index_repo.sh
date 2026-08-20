#!/usr/bin/env bash
# Index a Linux C/C++ tree and write HTML viz next to the DB.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${1:-}"
if [[ -z "$REPO" ]]; then
  echo "usage: $0 /path/to/linux/repo [extra index args...]" >&2
  exit 2
fi
shift || true

REPO="$(cd "$REPO" && pwd)"
DB="$REPO/.cpp-coro-graph/graph.db"
HTML="$REPO/.cpp-coro-graph/graph.html"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python3 -m cpp_coro_graph index "$REPO" --db "$DB" "$@"
python3 -m cpp_coro_graph viz --db "$DB" --out "$HTML"
python3 -m cpp_coro_graph status --db "$DB"
echo "HTML: $HTML"
