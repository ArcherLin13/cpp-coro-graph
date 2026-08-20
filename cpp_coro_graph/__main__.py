"""Entry: print immediately, then import CLI (avoids silent hang on slow imports)."""

from __future__ import annotations

import sys


def _boot() -> None:
    print("[cpp-coro-graph] boot: __main__ entered", file=sys.stderr, flush=True)
    print("[cpp-coro-graph] boot: importing cli …", file=sys.stderr, flush=True)
    from cpp_coro_graph.cli import main

    print("[cpp-coro-graph] boot: cli imported, calling main()", file=sys.stderr, flush=True)
    main()


if __name__ == "__main__":
    _boot()
