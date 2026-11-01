"""Measures drift detection on simulated latency and cost streams, plus tracer overhead.

Streams are simulated with known ground truth. They are not production traffic, so these numbers
describe the detector under the stated distributions.
"""
import json
import pathlib
import random
import tempfile
import time

from llmtrace import Tracer
from llmtrace.analysis import drift


def window(rng, n, latency, tokens, error_rate):
    rows = []
    for _ in range(n):
        out = rng.lognormvariate(tokens, 0.5)
        rows.append(dict(seconds=rng.lognormvariate(latency, 0.4), cost=out * 5e-6, output_tokens=int(out), input_tokens=500 + rng.randint(-50, 50),
                         output_chars=int(out * 4), status="error" if rng.random() < error_rate else "ok"))
    return rows


SCENARIOS = {
    "No change": (0.0, 0.0, 0.0),
    "Latency +10%": (0.0953, 0.0, 0.0),
    "Latency +25%": (0.2231, 0.0, 0.0),
    "Latency +50%": (0.4055, 0.0, 0.0),
    "Output length +25%": (0.0, 0.2231, 0.0),
    "Output length +50%": (0.0, 0.4055, 0.0),
    "Errors 1% to 3%": (0.0, 0.0, 0.02),
    "Errors 1% to 6%": (0.0, 0.0, 0.05),
}


def main(trials=500):
    results = {}
    for n in (100, 500):
        rows = {}
        for name, (dl, dt, de) in SCENARIOS.items():
            rng = random.Random(sum(map(ord, name)) + n)
            fired = sum(bool(drift(window(rng, n, 0.0, 5.0, 0.01), window(rng, n, dl, 5.0 + dt, 0.01 + de))) for _ in range(trials))
            rows[name] = fired / trials
            print(n, name, rows[name], flush=True)
        results[f"window_{n}"] = rows
    with tempfile.TemporaryDirectory() as folder:
        t = Tracer(f"{folder}/t.db", prices={"m": (1, 5)})
        count = 100000
        start = time.perf_counter()
        for _ in range(count):
            with t.llm("c", "m") as span:
                span.record(10, 10, output="x")
        per_span = (time.perf_counter() - start) / count
        t.close()
    results["microseconds_per_span"] = per_span * 1e6
    results["trials"] = trials
    print(per_span * 1e6, "microseconds per span")
    pathlib.Path("benchmarks/results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
