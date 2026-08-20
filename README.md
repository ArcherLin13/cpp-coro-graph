# cpp-coro-graph (V1)

Syntax-level **C++17 coroutine + device-domain** graph.  
**No `compile_commands.json`.** No CodeGraph fork. Stdlib-only Python.

What V1 can do:

- Find `exec::task<…> Name` / functions with `co_await` / `co_return`
- Draw **`await`** edges from `co_await Foo(…)`
- Tag **device domains** (`cpu` / `gpu` / `npu` / `dsp`) via `rules/devices.json`
- SQLite DB + HTML visualization + minimal MCP (`coro_explore`, `coro_stats`)

What V1 cannot do:

- Template / macro / overload resolution (needs compile_commands later)
- Cross-thread resume / true parallel edges from time
- Perfect C++ parsing (regex + brace matching, not a compiler)

## Quick start

```bash
# from this directory
python -m cpp_coro_graph index /path/to/your/repo
python -m cpp_coro_graph viz --db /path/to/your/repo/.cpp-coro-graph/graph.db
# open the generated .html in a browser
```

Default DB: `<repo>/.cpp-coro-graph/graph.db`  
Default HTML: same folder `graph.html`

Export JSON:

```bash
python -m cpp_coro_graph export --db .../graph.db
python -m cpp_coro_graph status --db .../graph.db
```

Smoke on bundled fixture:

```bash
python -m cpp_coro_graph index fixtures/sample --db fixtures/sample/.cpp-coro-graph/graph.db
python -m cpp_coro_graph viz --db fixtures/sample/.cpp-coro-graph/graph.db
```

## Cursor MCP (optional)

Add to Cursor MCP settings (adjust paths):

```json
{
  "mcpServers": {
    "cpp-coro-graph": {
      "command": "python",
      "args": [
        "-m", "cpp_coro_graph", "mcp",
        "--db", "D:/path/to/your/repo/.cpp-coro-graph/graph.db"
      ],
      "cwd": "D:/WorkStation/CodeOptimization/cpp-coro-graph"
    }
  }
}
```

Tools: `coro_explore` (query symbol/file/domain), `coro_stats`.

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

`.git`, `build`, `out`, `third_party`, `node_modules`, `.codegraph`, …
