"""Records nested spans to SQLite. Writes are batched on a background thread so calls stay fast."""
import atexit
import contextvars
import json
import queue
import sqlite3
import threading
import time
import uuid

_current = contextvars.ContextVar("llmtrace_span", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (id TEXT PRIMARY KEY, trace TEXT, parent TEXT, name TEXT, kind TEXT, start REAL,
    seconds REAL, status TEXT, error TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost REAL,
    output_chars INTEGER, attrs TEXT);
CREATE INDEX IF NOT EXISTS spans_start ON spans (start);
CREATE INDEX IF NOT EXISTS spans_trace ON spans (trace);
"""
RETRYABLE = ("timeout", "ratelimit", "rate limit", "429", "503", "connection")


def classify_error(error):
    """Groups failures by what you'd do about them."""
    text = f"{type(error).__name__} {error}".lower()
    if any(t in text for t in ("jsondecode", "validation", "schema", "parse")):
        return "bad_output"
    if any(t in text for t in ("context length", "too long", "maximum context")):
        return "too_long"
    if any(t in text for t in ("401", "403", "authentication", "permission")):
        return "auth"
    if any(t in text for t in RETRYABLE):
        return "transient"
    return "other"


class Span:
    def __init__(self, tracer, name, kind, model):
        self.tracer, self.name, self.kind, self.model = tracer, name, kind, model
        parent = _current.get()
        self.id = uuid.uuid4().hex
        self.parent = parent.id if parent else None
        self.trace = parent.trace if parent else uuid.uuid4().hex
        self.input_tokens = self.output_tokens = 0
        self.output_chars = None
        self.attrs = {}

    def record(self, input_tokens=0, output_tokens=0, output=None, **attrs):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if output is not None:
            self.output_chars = len(output)
        self.attrs.update(attrs)

    def __enter__(self):
        self.start = time.time()
        self._clock = time.perf_counter()
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        seconds = time.perf_counter() - self._clock
        _current.reset(self._token)
        cost = self.tracer.price(self.model, self.input_tokens, self.output_tokens)
        self.tracer._queue.put((self.id, self.trace, self.parent, self.name, self.kind, self.start, seconds,
                                "error" if exc else "ok", classify_error(exc) if exc else None, self.model,
                                self.input_tokens, self.output_tokens, cost, self.output_chars,
                                json.dumps(self.attrs, default=str)))
        return False


class Tracer:
    def __init__(self, path="traces.db", prices=None):
        self.path, self.prices = str(path), prices or {}
        self._queue = queue.Queue()
        self._stop = object()
        self._idle = threading.Event()
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def price(self, model, input_tokens, output_tokens):
        rates = self.prices.get(model)
        if not rates:
            return 0.0
        return (input_tokens * rates[0] + output_tokens * rates[1]) / 1e6

    def span(self, name, kind="step", model=None):
        return Span(self, name, kind, model)

    def llm(self, name, model):
        return Span(self, name, "llm", model)

    def _writer(self):
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        while True:
            item = self._queue.get()
            batch, done = [], item is self._stop
            if not done:
                batch.append(item)
            while not done and len(batch) < 500:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is self._stop:
                    done = True
                else:
                    batch.append(item)
            if batch:
                db.executemany("INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                db.commit()
            if done:
                db.close()
                return

    def close(self):
        """Flushes everything to disk. Safe to call twice."""
        if self._thread.is_alive():
            self._queue.put(self._stop)
            self._thread.join()
