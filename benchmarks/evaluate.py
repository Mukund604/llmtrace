"""Measures drift detection on simulated latency and cost streams, plus tracer overhead.

Streams are simulated with known ground truth. They are not production traffic, so these numbers
describe the detector under the stated distributions.
"""
import json
import math
import pathlib
import random
import tempfile
import time

from llmtrace import Tracer
from llmtrace.analysis import drift

from .charts import save


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


def power_curves(trials):
    """Detection rate against the size of a latency shift, for several window sizes."""
    shifts = [0, 5, 10, 15, 20, 25, 30, 40, 50]
    sizes = [50, 100, 250, 500]
    table = {}
    for n in sizes:
        rng = random.Random(n)
        table[n] = [sum(bool(drift(window(rng, n, 0.0, 5.0, 0.01), window(rng, n, math.log(1 + s / 100), 5.0, 0.01))) for _ in range(trials)) / trials for s in shifts]
        print("power", n, table[n], flush=True)
    pathlib.Path("benchmarks/power.json").write_text(json.dumps(dict(shifts=shifts, table=table, trials=trials), indent=2))

    def draw(fig, theme):
        ax = fig.add_subplot()
        for i, n in enumerate(sizes):
            ax.plot(shifts, [v * 100 for v in table[n]], color=theme["series"][i], lw=2, marker="o", ms=6, label=f"{n} calls per window")
        ax.set_xlabel("True rise in latency (%)")
        ax.set_ylabel("Alert fired (%)")
        ax.set_ylim(-3, 105)
        ax.set_title("How big a latency change the drift alert can see")
        ax.legend(loc="lower right")
    save(draw, "power")


def demo():
    """Simulated month with an injected incident, used only for the dashboard screenshot."""
    rng = random.Random(7)
    path = pathlib.Path("results/demo.db")
    path.unlink(missing_ok=True)
    import sqlite3
    t = Tracer(path)
    t.close()
    db = sqlite3.connect(path)
    day = 86400
    start = 1_790_000_000 // day * day
    rows = []
    for d in range(30):
        incident = d == 29
        for i in range(600):
            out = rng.lognormvariate(5.3 if incident else 5.0, 0.5)
            latency = rng.lognormvariate(0.5 if incident else 0.0, 0.4)
            bad = rng.random() < (0.05 if incident else 0.01)
            rows.append((f"{d}-{i}", f"{d}-{i}", None, "generate", "llm", start + d * day + i * 100, latency, "error" if bad else "ok",
                         rng.choice(["transient", "transient", "bad_output"]) if bad else None, "simulated-model", 500, int(out), (500 + 5 * out) / 1e6, int(out * 4), "{}"))
    db.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    from llmtrace.dashboard import render
    pathlib.Path("results/demo-dashboard.html").write_text(render(path, "Simulated data with an injected incident on the last day"), encoding="utf-8")
    path.unlink(missing_ok=True)


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
    power_curves(trials)
    demo()


if __name__ == "__main__":
    main()
