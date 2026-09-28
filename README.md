# FrameX-CLI

FrameX is a command-line reasoning engine for F-Logic: load a program, ask
questions of it, and get back not just answers but explanations. It's
written in Rust, with a Python client for scripting and integration into
agent tool chains.

## Getting Started

1. **Get the Python framework.** Download the [`framex`](framex) folder from
   this repo. It contains `framex.py` — the Python client — and its own
   [README](framex/README.md) describing the functions the framework
   provides.
2. **Install the engine binary.** Download the latest `framex` binary from
   the [Releases](../../releases) tab, and follow the `INSTALL.md`
   instructions included in the zip file.

Once both are in place, `framex.py` starts the engine as a subprocess and
talks to it over JSON Lines — see the [Python client
README](framex/README.md) for a quick-start example.

## Documentation

For general information about FrameX, see the [FrameX
Documentation](https://unisg-ics-dsnlp.github.io/FrameX-Doc/).
