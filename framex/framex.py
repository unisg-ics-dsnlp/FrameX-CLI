"""Standard-library Python client for the FrameX engine.

All reasoning happens in the `framex` binary. This module is only a transport:
it starts `framex serve` as a subprocess and exchanges JSON Lines with it over
stdin/stdout. There is no inference here, and no third-party dependency.

    from framex import Client

    with Client() as client:
        client.load("world open. socrates:Human. ?X:Mortal <- ?X:Human.")
        print(client.query("?- socrates:Mortal."))   # {'status': 'true'}

Requires Python 3.10+ and the `framex` binary on PATH (or pass its path).
"""

import json
import math
import queue
import subprocess
import threading
import time

__all__ = ["Client", "FrameXError", "FrameXTimeout", "READ_TOOLS", "call_tool"]

# The engine refuses a request line larger than this, so every request must fit.
MAX_REQUEST_BYTES = 1_048_576
# Leaves room for the JSON envelope and escaping around a chunk of source text.
DEFAULT_CHUNK_BYTES = 700_000


def _invalid_constant(value):
    raise ValueError("non-finite number in FrameX response")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("number outside finite float range in FrameX response")
    return number


class FrameXError(RuntimeError):
    """The engine rejected the request. `.error` holds kind, message and location."""

    def __init__(self, error):
        self.error = error
        super().__init__(f"{error.get('kind')}: {error.get('message')}")


class FrameXTimeout(TimeoutError):
    """The process was terminated; a sent mutation may have had an unknown outcome."""


def _statement_chunks(text, limit):
    """Split source into pieces of at most `limit` bytes.

    A cut is only made after a line whose content ends a statement ('.'), so a
    rule written over several lines is never split in half. A piece may exceed
    `limit` when no statement ends in time; `request` still refuses anything
    over 1 MiB, so an unsplittable program fails loudly rather than silently.
    """
    piece, size, pending = [], 0, False
    for line in text.splitlines(keepends=True):
        piece.append(line)
        size += len(line.encode("utf-8"))
        # Ignore a trailing line comment when deciding whether a statement ended.
        content = line.split("//", 1)[0].rstrip()
        pending = pending or bool(content)
        if size >= limit and content.endswith("."):
            yield "".join(piece)
            piece, size, pending = [], 0, False
    if pending or piece:
        remainder = "".join(piece)
        if remainder.strip():
            yield remainder


class Client:
    """A running `framex serve` process.

    binary            path to the FrameX executable (default: `framex` on PATH)
    request_timeout   seconds to wait for one response before killing the process
    retain_transcript keep every sent request in memory for `transcript` and
                      `save_replay`. Set False for large loads: the transcript
                      would otherwise hold a second copy of the whole program.
    """

    def __init__(self, binary="framex", *, request_timeout=30, retain_transcript=True,
                 max_response_bytes=8 * 1024 * 1024, max_transcript_bytes=16 * 1024 * 1024):
        if not isinstance(request_timeout, (int, float)) or not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be finite and positive")
        if any(type(n) is not int or n <= 0 for n in (max_response_bytes, max_transcript_bytes)):
            raise ValueError("response and transcript limits must be positive integers")
        self.request_timeout = request_timeout
        self.retain_transcript = bool(retain_transcript)
        self.max_response_bytes = max_response_bytes
        self.max_transcript_bytes = max_transcript_bytes
        self._transcript_bytes = 0
        self._broken = False
        self._worker = None
        try:
            self.process = subprocess.Popen(
                [str(binary), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"FrameX binary {binary!r} not found. Put `framex` on PATH, or pass "
                f"Client(binary='/path/to/framex')."
            ) from None
        self._id = 0
        self._lock = threading.Lock()
        self.last_response = None
        self._transcript = []

    # --- transport ------------------------------------------------------

    def _abort(self):
        self._broken = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def request(self, command, *, _record=None, **fields):
        """Send one command and return its `result`. Raises FrameXError if refused."""
        deadline = time.monotonic() + self.request_timeout
        if not self._lock.acquire(timeout=self.request_timeout):
            raise FrameXTimeout("client busy; this request was not sent")
        try:
            if self._broken:
                raise RuntimeError(
                    "client is closed or failed; create a new session; "
                    "do not retry uncertain mutations")
            self._id += 1
            request = dict(fields, command=command, id=self._id)
            encoded = json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n"
            size = len(encoded.encode())
            if size > MAX_REQUEST_BYTES:
                raise ValueError(
                    "request exceeds 1 MiB; use load_program() to send a large "
                    "program in chunks")
            record = self.retain_transcript if _record is None else _record
            if record:
                if self._transcript_bytes + size > self.max_transcript_bytes:
                    raise ValueError(
                        "transcript limit reached; use Client(retain_transcript=False) "
                        "for large loads, or export the replay and start a new session")
                # Retain attempted requests, including an uncertain timed-out mutation.
                self._transcript.append(encoded)
                self._transcript_bytes += size
            replies = queue.Queue(maxsize=1)

            def exchange():
                try:
                    self.process.stdin.write(encoded)
                    self.process.stdin.flush()
                    line = self.process.stdout.readline(self.max_response_bytes + 1)
                    if len(line.encode()) > self.max_response_bytes or not line.endswith("\n"):
                        raise RuntimeError("missing, incomplete or oversized FrameX response")
                    replies.put((json.loads(line, parse_constant=_invalid_constant,
                                            parse_float=_finite_float), None))
                except Exception as exc:
                    replies.put((None, exc))

            self._worker = threading.Thread(target=exchange, daemon=True)
            self._worker.start()
            try:
                response, failure = replies.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self._abort()
                raise FrameXTimeout(
                    "FrameX request timed out; process terminated; mutation outcome "
                    "unknown; no automatic retry") from None
            if failure is not None:
                self._abort()
                raise failure
            if (not isinstance(response, dict)
                    or type(response.get("id")) is not int or response["id"] != self._id
                    or type(response.get("ok")) is not bool
                    or (response["ok"] and "result" not in response)
                    or (not response["ok"] and (
                        not isinstance(response.get("error"), dict)
                        or not isinstance(response["error"].get("kind"), str)
                        or not isinstance(response["error"].get("message"), str)))):
                self._abort()
                raise RuntimeError("FrameX response ID or shape mismatch")
            self.last_response = response
            if not response.get("ok"):
                raise FrameXError(response["error"])
            return response["result"]
        finally:
            self._lock.release()

    # --- loading --------------------------------------------------------

    def load(self, source):
        """Replace the session with this program. One request, so under 1 MiB."""
        return self.request("load", source=source)

    def add(self, *, source):
        """Add facts/rules to the session and return the derived diff."""
        return self.request("add", source=source)

    def load_begin(self, **limits):
        """Open a staged bulk load. Optional max_rounds/max_facts/max_matches/max_proofs."""
        return self.request("load_begin", **limits)

    def load_chunk(self, source, *, file=None, record=None):
        """Stage one piece. Nothing is published and no inference runs yet."""
        fields = {"source": source}
        if file is not None:
            fields["file"] = file
        return self.request("load_chunk", _record=record, **fields)

    def load_commit(self):
        """Build the staged program once and publish it, or fail and change nothing."""
        return self.request("load_commit")

    def load_abort(self):
        """Discard staged work. Aborting with nothing staged is not an error."""
        return self.request("load_abort")

    def load_program(self, source=None, *, path=None, file=None,
                     chunk_bytes=DEFAULT_CHUNK_BYTES, record=False, **limits):
        """Load a program of any size, in one atomic transaction.

        Pass `source` text or `path` to a file. The program is split into pieces
        that each fit in one request, staged with load_chunk, and built once by
        load_commit. The engine parses and accumulates the pieces without
        rebuilding its indexes or deriving anything until the commit, which is
        far cheaper than repeated `add` calls and is atomic: if any piece or the
        commit fails, the previously loaded session is left untouched.

        The pieces are not written to the transcript by default (`record=False`),
        because a replay can re-read the source instead of holding a second copy
        of the whole program in memory.

        Returns the load_commit result, e.g. {'facts': 1030000}.
        """
        if (source is None) == (path is None):
            raise ValueError("pass exactly one of source= or path=")
        if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= MAX_REQUEST_BYTES:
            raise ValueError(f"chunk_bytes must be an integer in 1..{MAX_REQUEST_BYTES}")
        if path is not None:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            if file is None:
                file = str(path)
        self.load_begin(**limits)
        try:
            staged = 0
            for piece in _statement_chunks(source, chunk_bytes):
                self.load_chunk(piece, file=file, record=record)
                staged += 1
            if staged == 0:
                raise ValueError("program is empty; nothing to load")
            return self.load_commit()
        except BaseException:
            # Leave no staged program behind on the server we are still talking to.
            if not self._broken:
                try:
                    self.load_abort()
                except Exception:
                    pass
            raise

    # --- querying and inspection ----------------------------------------

    def query(self, query):
        return self.request("query", query=query)

    def explain(self, fact):
        return self.request("explain", fact=fact)

    def why_not(self, fact):
        return self.request("why_not", fact=fact)

    def dependents(self, fact):
        return self.request("dependents", fact=fact)

    def schema(self):
        return self.request("schema")

    def modules(self):
        return self.request("modules")

    def validate(self):
        return self.request("validate")

    def stats(self):
        return self.request("stats")

    def diff(self):
        return self.request("diff")

    # --- mutation -------------------------------------------------------

    def retract(self, *, fact):
        return self.request("retract", fact=fact)

    def retract_source(self, source):
        return self.request("retract_source", source=source)

    def world(self, mode):
        """Switch the world assumption ('open' or 'closed') and recompute."""
        return self.request("world", mode=mode)

    def run(self):
        return self.request("run")

    def compare(self, *, facts, before, after):
        return self.request("compare", facts=facts, before=before, after=after)

    def provenance(self, fact, algebra, **options):
        return self.request("provenance", fact=fact, algebra=algebra, **options)

    def rdf_import(self, *, source, format, document, module=None):
        fields = dict(source=source, format=format, document=document)
        if module is not None:
            fields["module"] = module
        return self.request("rdf_import", **fields)

    def rdf_export(self, *, format, selection, module=None, predicate="rdf_quad"):
        fields = dict(format=format, selection=selection, predicate=predicate)
        if module is not None:
            fields["module"] = module
        return self.request("rdf_export", **fields)

    # --- transcript -----------------------------------------------------

    @property
    def transcript(self):
        """Detached view; caller edits cannot change recorded wire requests."""
        with self._lock:
            snapshot = tuple(self._transcript)
        return [json.loads(line) for line in snapshot]

    def save_replay(self, path):
        """Write the recorded requests as JSON Lines. Fails if the file exists."""
        with self._lock:
            snapshot = tuple(self._transcript)
        with open(path, "x", encoding="utf-8") as out:
            out.writelines(snapshot)

    # --- lifecycle ------------------------------------------------------

    def close(self):
        self._broken = True
        # Closing buffered handles while a reader owns them can itself block.
        if self._worker and self._worker.is_alive():
            self._abort()
            self._worker.join(timeout=1)
        if not self._worker or not self._worker.is_alive():
            if self.process.stdin and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._abort()
            if self.process.stdout:
                self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Minimal agent allowlist: no load, mutation, world switch or arbitrary shell execution.
READ_TOOLS = {
    "query": {"query": "F-Logic query using declared classes and named slots"},
    "explain": {"fact": "Ground FrameX fact"},
    "why_not": {"fact": "Ground FrameX fact"},
    "schema": {},
    "modules": {},
    "validate": {},
}


def call_tool(client, name, arguments):
    """Call one read-only command by name, for tool-using agents."""
    if name not in READ_TOOLS or set(arguments) != set(READ_TOOLS[name]):
        raise ValueError("unsupported tool or arguments")
    return client.request(name, **arguments)
