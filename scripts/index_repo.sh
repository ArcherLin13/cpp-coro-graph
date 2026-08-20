#!/usr/bin/env bash
# Index a Linux C/C++ tree and write HTML viz next to the DB.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${1:-}"
if [[ -z "${REPO}" ]]; then
  echo "usage: $0 /path/to/linux/repo [extra index args...]" >&2
  exit 2
fi
shift || true

if [[ ! -d "${REPO}" ]]; then
  echo "not a directory: ${REPO}" >&2
  exit 2
fi

REPO="$(cd "${REPO}" && pwd)"
DB="${REPO}/.cpp-coro-graph/graph.db"
HTML="${REPO}/.cpp-coro-graph/graph.html"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "python3 not found" >&2
  exit 127
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
"${PY}" -m cpp_coro_graph index "${REPO}" --db "${DB}" "$@"
"${PY}" -m cpp_coro_graph viz --db "${DB}" --out "${HTML}"
"${PY}" -m cpp_coro_graph status --db "${DB}"
echo "HTML: ${HTML}"
