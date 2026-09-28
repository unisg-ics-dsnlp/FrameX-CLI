# FrameX Python API

A single-file Python client for the FrameX reasoning engine.

All reasoning happens in the `framex` binary. This client is only a transport: it
starts `framex serve` as a subprocess and exchanges JSON Lines with it. There is
no inference in Python, and no third-party dependency — only the standard library.

## Contents

| File | Purpose |
|---|---|
| `framex.py` | the client — this is the only file you need |

## Requirements

- Python 3.10 or newer
- the `framex` binary

## Install

Copy `framex.py` next to your own code:

```sh
cp framex.py ~/my-project/
```

Then make sure the engine is reachable. Either put `framex` on your `PATH`:

```sh
framex --version          # framex <version> (commit ...)
```

or pass the path explicitly:

```python
client = Client(binary="/path/to/framex")
```

## Quick start

```python
from framex import Client

with Client() as client:
    client.load("""
        world open.
        socrates:Human.
        ?X:Mortal <- ?X:Human.
    """)
    print(client.query("?- socrates:Mortal."))   # {'status': 'true'}
    print(client.query("?- plato:Mortal."))      # {'status': 'unknown'}
    print(client.explain("socrates:Mortal"))     # the rule and the fact it used
```

Use the client as a context manager (`with`), or call `close()` yourself. Each
`Client` owns one engine process and one session.

Every program must declare its world assumption — `world open.` or
`world closed.` — and the engine refuses a program that does not. `open` answers
`unknown` when nothing is known; `closed` answers `false`. Nothing is assumed
implicitly.

## Loading

| Method | Use |
|---|---|
| `load(source)` | replace the session with this program (must fit in one 1 MiB request) |
| `load_program(source=..., path=...)` | **load a program of any size** |
| `add(source=...)` | add facts or rules to the loaded session, returns the derived diff |

A request is limited to 1 MiB, so a larger program cannot be sent with `load`.
`load_program` handles that for you:

```python
with Client() as client:
    result = client.load_program(path="big-program.fx")
    print(result)          # {'facts': 1030000}
```

It splits the source into pieces, stages them with `load_chunk`, and has the
engine build the program once with `load_commit`. The engine parses and
accumulates the pieces without rebuilding its indexes or deriving anything until
the commit.

**Do not loop over `add()` to load a large program.** Each `add` rebuilds and
re-derives the whole knowledge base, so the cost grows quadratically with the
number of pieces. Measured through this client on 800,000 facts (18 MB) split
into 91 pieces: about 73 s and 3.4 GB with repeated `add`, against 3.5 s and
2.2 GB with `load_program`.

`load_program` is atomic. If any piece fails to parse, or the commit fails to
build, the previously loaded session is left exactly as it was and the staged
work is discarded:

```python
client.load("world open. keep:p.")
try:
    client.load_program("oops:p.")          # no world declaration
except FrameXError:
    pass
client.query("?- keep:p.")                  # still {'status': 'true'}
```

Pieces are cut only after a line that ends a statement, so a rule written over
several lines is never split in half.

For very large programs, switch the transcript off so the client does not hold a
second copy of your program in memory:

```python
client = Client(retain_transcript=False)
```

`load_program` already keeps its own chunks out of the transcript; pass
`record=True` if you want them recorded.

## Asking questions

| Method | Returns |
|---|---|
| `query(q)` | `{'status': 'true'\|'false'\|'unknown'}`, or bindings for a variable query |
| `explain(fact)` | how the fact was derived, down to asserted facts and their sources |
| `why_not(fact)` | why a fact could *not* be derived, with the blocking conditions |
| `dependents(fact)` | the facts that were derived using this one |
| `schema()` | the query patterns the loaded program supports |
| `validate()` | schema and constraint diagnostics |
| `stats()` | rounds, derived facts, rule firings, elapsed time |

```python
client.query("?- ?X:Mortal.")
# {'status': 'bindings', 'bindings': [{'X': 'socrates'}]}
```

## Changing the session

| Method | Effect |
|---|---|
| `retract(fact=...)` | withdraw one asserted fact and recompute |
| `retract_source(source)` | withdraw everything from one origin and recompute |
| `world(mode)` | switch to `'open'` or `'closed'` and recompute |
| `run()` | recompute the fixpoint |
| `diff()` | the facts added and removed by the last mutation |

Every mutation recomputes atomically: if it fails, the session is unchanged.

## Errors

```python
from framex import Client, FrameXError, FrameXTimeout
```

- `FrameXError` — the engine refused the request. `error.error` holds `kind`,
  `message`, `file`, `line` and `column`.
- `FrameXTimeout` — no reply within `request_timeout` (default 30 s). The process
  is killed, and the outcome of a mutation that was already sent is unknown, so
  the client refuses further use. Create a new `Client`.
- `ValueError` — the request was malformed before being sent, for example larger
  than 1 MiB.

```python
try:
    client.load("a:p.")
except FrameXError as failure:
    print(failure.error["kind"])      # SemanticError
    print(failure.error["message"])   # missing mandatory world declaration...
```

## Reproducing a session

The client records every request it sends:

```python
client.transcript                 # list of request dicts
client.save_replay("run.jsonl")   # JSON Lines, refuses to overwrite
```

Replay a saved file straight into the engine:

```sh
framex serve < run.jsonl
```

## Tool-using agents

`READ_TOOLS` and `call_tool` expose a read-only subset — `query`, `explain`,
`why_not`, `schema`, `modules`, `validate` — with no loading, mutation or world
switch, for handing to an agent:

```python
from framex import call_tool
call_tool(client, "query", {"query": "?- socrates:Mortal."})
call_tool(client, "load", {"source": "..."})     # ValueError: unsupported tool
```

## Scope

This client covers loading, querying, explanation and session mutation. The
engine also speaks commands for RDF import/export, program comparison, proof
algebras and the evidence ledger; `client.request("command", **fields)` reaches
any of them directly, and `compare`, `provenance`, `modules` and the RDF calls
have named methods.
