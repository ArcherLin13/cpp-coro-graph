# cpp-coro-graph (V1)

Syntax-level **C++17 coroutine + device-domain** graph.  
**Primary target: Linux.** Stdlib-only Python 3.9+ (no pip deps required to run).

What V1 can do:

- **All functions as nodes**: definitions `{...}` **and** declarations `Foo();` / `= default` / `= delete` (headers included)
- Class/struct members qualified as `Class::Method` (incl. ctor/dtor)
- Find `exec::task<…> Name` / functions with `co_await` / `co_return`
- Draw **`await`** edges from `co_await Foo(…)` plus normal **`calls`**
- **Member pipeline**: `co_await m.Init()` inside `SosModel::Call` resolves to `SosModel::Init` (enclosing class + local `SosModel m`)
- Tag **device domains** (`cpu` / `gpu` / `npu` / `dsp`) via `rules/devices.json`
- SQLite DB + HTML visualization + CLI (`query` / `callers` / `callees` / `explore`) + MCP

What V1 cannot do:

- Template / macro / overload resolution (needs compile_commands later)
- Cross-thread resume / true parallel edges from time
- Perfect C++ parsing (regex + brace matching, not a compiler)

## Install on Linux

```bash
git clone https://github.com/ArcherLin13/cpp-coro-graph.git
cd cpp-coro-graph

# option A — no install (recommended for a quick try)
chmod +x scripts/cpp-coro-graph scripts/index_repo.sh
./scripts/cpp-coro-graph index /path/to/your/repo

# option B — install CLI on PATH
python3 -m pip install -e .
cpp-coro-graph index /path/to/your/repo
```

Requires only `python3` (3.9+). Uses the stdlib `sqlite3` module.

## Index your Linux code tree

```bash
# one-shot: index + HTML + status
./scripts/index_repo.sh /path/to/your/linux/repo

# or step by step
python3 -m cpp_coro_graph index /path/to/your/linux/repo
python3 -m cpp_coro_graph viz --db /path/to/your/linux/repo/.cpp-coro-graph/graph.db
python3 -m cpp_coro_graph status --db /path/to/your/linux/repo/.cpp-coro-graph/graph.db
```

Outputs:

- `/path/to/your/linux/repo/.cpp-coro-graph/graph.db`
- `/path/to/your/linux/repo/.cpp-coro-graph/graph.html` (open in a browser)

Run the tool **on the Linux machine (or WSL2 Linux filesystem)** that holds the sources. Avoid indexing via `/mnt/c/...` from WSL.

Smoke test:

```bash
./scripts/cpp-coro-graph index fixtures/sample --db fixtures/sample/.cpp-coro-graph/graph.db
./scripts/cpp-coro-graph viz --db fixtures/sample/.cpp-coro-graph/graph.db
```

## Query the graph (codegraph-style)

Large graphs (10k+ nodes) — **prefer these over HTML**:

```bash
DB=/path/to/repo/.cpp-coro-graph/graph.db

# search symbols
python3 -u -m cpp_coro_graph -q query OnSos --db "$DB"

# who calls / awaits this
python3 -u -m cpp_coro_graph -q callers OnSos --db "$DB"

# what it calls / awaits / spawns
python3 -u -m cpp_coro_graph -q callees OnSos --db "$DB" --edge-kind calls,await,spawns

# neighborhood BFS
python3 -u -m cpp_coro_graph -q explore OnSos --depth 2 --db "$DB"

# incoming blast radius
python3 -u -m cpp_coro_graph -q impact OnSos --depth 2 --db "$DB"

# JSON for scripting
python3 -u -m cpp_coro_graph -q callers OnSos --db "$DB" --json
```

If you `cd` into the indexed repo, `--db` can be omitted (searches upward for `.cpp-coro-graph/graph.db`).

## Cursor MCP (optional)

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

Different questions → different edge types. Filter with `--edge-kind` / viz dropdown.

| kind | layer | meaning | example |
|------|-------|---------|---------|
| `await` | control | coroutine suspends on target | `Caller --await--> A` |
| **`seq`** | **order** | **same body, consecutive `co_await` order** | `A --seq--> DoCpuWork` (inside `Caller`) |
| `calls` | control | normal / sync call | `Foo --calls--> Bar` |
| `spawns` | schedule | starts work on another thread/task | `std::thread`, `co_spawn` |
| `handoff` | schedule | posts/schedules to executor | `PostTask`, `asio::post` |
| `device_call` | device | hit a device API pattern | `clEnqueue…` |
| **`contains`** | **structure** | **file owns this function** | `file:demo.cpp --contains--> Caller` |

- Want **顺序**：`--edge-kind seq`（或 `seq,await`）
- Want **文件归属**：`--edge-kind contains`
- Want **谁等谁**：`--edge-kind await`

`seq` metadata `raw` records which coroutine (`in Caller #0`).

## Skipped directories

`.git`, `build`, `out`, `third_party`, `node_modules`, `.codegraph`, `bazel-*`, `.repo`, `prebuilts`, …
