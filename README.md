# cpp-coro-graph (V1)

Syntax-level **C++17 coroutine + device-domain** graph for **Linux C/C++ trees**.  
**No `compile_commands.json`.** Stdlib-only Python 3.

What V1 can do:

- Find `exec::task<…> Name` / functions with `co_await` / `co_return`
- Draw **`await`** edges from `co_await Foo(…)`
- Tag **device domains** (`cpu` / `gpu` / `npu` / `dsp`) via `rules/devices.json`
- SQLite DB + HTML visualization + minimal MCP (`coro_explore`, `coro_stats`)

What V1 cannot do:

- Template / macro / overload resolution (needs compile_commands later)
- Cross-thread resume / true parallel edges from time
- Perfect C++ parsing (regex + brace matching, not a compiler)

## Requirements

- Linux (or WSL2 with the repo **inside the Linux filesystem**, not `/mnt/c/...`)
- Python 3.9+ (`python3`)

Run the indexer **on the same machine/OS that holds the source tree**. Indexing a Linux checkout through a Windows path mount is slow and error-prone.

## Quick start (Linux)

```bash
# clone this tool
git clone https://github.com/ArcherLin13/cpp-coro-graph.git
cd cpp-coro-graph

# index your Linux code repo
python3 -m cpp_coro_graph index /path/to/your/linux/repo

# HTML graph
python3 -m cpp_coro_graph viz --db /path/to/your/linux/repo/.cpp-coro-graph/graph.db
# open: /path/to/your/linux/repo/.cpp-coro-graph/graph.html
```

Or:

```bash
chmod +x scripts/index_repo.sh
./scripts/index_repo.sh /path/to/your/linux/repo
```

Default DB: `<repo>/.cpp-coro-graph/graph.db`  
Default HTML: `<repo>/.cpp-coro-graph/graph.html`

Useful:

```bash
python3 -m cpp_coro_graph status --db /path/to/repo/.cpp-coro-graph/graph.db
python3 -m cpp_coro_graph export --db /path/to/repo/.cpp-coro-graph/graph.db
```

Smoke test:

```bash
python3 -m cpp_coro_graph index fixtures/sample --db fixtures/sample/.cpp-coro-graph/graph.db
python3 -m cpp_coro_graph viz --db fixtures/sample/.cpp-coro-graph/graph.db
```

## Cursor MCP (optional)

Index on Linux first, then point MCP at that DB (paths must be reachable from the Cursor host — e.g. WSL or remote SSH):

```json
{
  "mcpServers": {
    "cpp-coro-graph": {
      "command": "python3",
      "args": [
        "-m", "cpp_coro_graph", "mcp",
        "--db", "/path/to/your/linux/repo/.cpp-coro-graph/graph.db"
      ],
      "cwd": "/path/to/cpp-coro-graph"
    }
  }
}
```

Tools: `coro_explore`, `coro_stats`.

## Custom device rules

Edit `rules/devices.json` or pass `--rules your.json`:

```json
{
  "patterns": [
    {"match": "RunOnNpu", "domain": "npu", "backend": "custom"},
    {"match": "clEnqueue", "domain": "gpu", "backend": "opencl"}
  ]
}
```

First match wins. Put longer / more specific strings first.

## Edge kinds

| kind | meaning |
|------|---------|
| `await` | `co_await Target` in a coroutine body |
| `device_call` | body text hit a device API pattern |

Node colors in HTML = domain. Red edges = await.

## Skipped directories

`.git`, `build`, `out`, `third_party`, `node_modules`, `.codegraph`, `bazel-*`, `.repo`, `output`, …
