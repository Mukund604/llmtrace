import json
import random
import threading

import pytest

from llmtrace import Tracer
from llmtrace.analysis import drift, ks_statistic, llm_rows, percentile, summary
from llmtrace.cli import main
from llmtrace.tracer import classify_error


def tracer(tmp_path):
    return Tracer(tmp_path / "t.db", prices={"m": (1.0, 5.0)}), tmp_path / "t.db"


def test_records_cost_tokens_and_output(tmp_path):
    t, path = tracer(tmp_path)
    with t.llm("call", "m") as span:
        span.record(input_tokens=1000, output_tokens=200, output="hello", temperature=0)
    t.close()
    row = llm_rows(path)[0]
    assert row["cost"] == pytest.approx((1000 + 1000) / 1e6)
    assert row["output_chars"] == 5 and json.loads(row["attrs"]) == {"temperature": 0}


def test_nesting_shares_a_trace(tmp_path):
    t, path = tracer(tmp_path)
    with t.span("outer") as outer:
        with t.llm("inner", "m") as inner:
            pass
    with t.llm("separate", "m") as other:
        pass
    t.close()
    assert inner.trace == outer.trace and inner.parent == outer.id
    assert other.trace != outer.trace and other.parent is None


def test_threads_do_not_cross_contaminate_traces(tmp_path):
    t, _ = tracer(tmp_path)
    found = {}

    def worker(i):
        with t.span(f"o{i}") as outer:
            with t.span(f"i{i}") as inner:
                found[i] = inner.parent == outer.id

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    t.close()
    assert all(found.values()) and len(found) == 16


def test_exception_is_recorded_and_still_raised(tmp_path):
    t, path = tracer(tmp_path)
    with pytest.raises(TimeoutError):
        with t.llm("call", "m"):
            raise TimeoutError("timed out")
    t.close()
    row = llm_rows(path)[0]
    assert row["status"] == "error" and row["error"] == "transient"


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("x"), "transient"), (Exception("HTTP 429"), "transient"),
    (ValueError("JSON parse failed"), "bad_output"), (Exception("maximum context length"), "too_long"),
    (Exception("401 unauthorized"), "auth"), (KeyError("what"), "other"),
])
def test_classify_error(error, expected):
    assert classify_error(error) == expected


def test_close_twice_and_no_loss_under_load(tmp_path):
    t, path = tracer(tmp_path)
    for _ in range(3000):
        with t.llm("c", "m"):
            pass
    t.close()
    t.close()
    assert len(llm_rows(path)) == 3000


def test_ks_statistic():
    assert ks_statistic([1, 2, 3], [1, 2, 3]) == 0
    assert ks_statistic([1, 2], [3, 4]) == 1


def test_percentile_and_empty_summary():
    assert percentile([1, 2, 3, 4], .5) == 3
    assert summary([])["calls"] == 0


def rows(n, mean, seed, error_rate=0.0):
    rng = random.Random(seed)
    return [dict(seconds=rng.gauss(mean, mean / 5), cost=0.001, output_tokens=None, input_tokens=None, output_chars=None,
                 status="error" if rng.random() < error_rate else "ok") for _ in range(n)]


def test_drift_detects_shift_and_ignores_noise():
    assert drift(rows(300, 1, 0), rows(300, 1, 1)) == []
    assert drift(rows(300, 1, 0), rows(300, 2, 1))[0]["metric"] == "seconds"


def test_drift_needs_enough_samples():
    assert drift(rows(10, 1, 0), rows(10, 5, 1)) == []


def test_error_rate_rise_alerts_but_improvement_does_not():
    assert any(a["metric"] == "error_rate" for a in drift(rows(500, 1, 0, 0.01), rows(500, 1, 1, 0.15)))
    assert not any(a["metric"] == "error_rate" for a in drift(rows(500, 1, 0, 0.15), rows(500, 1, 1, 0.01)))


def test_cli(tmp_path, capsys):
    t, path = tracer(tmp_path)
    with t.llm("c", "m") as span:
        span.record(1, 1)
    t.close()
    assert main(["report", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 1
    assert main(["drift", str(path)]) == 0


def test_dashboard_is_self_contained(tmp_path, capsys):
    t, path = tracer(tmp_path)
    for name in ("<b>", "x"):
        with t.llm(name, "m") as span:
            span.record(10, 10, output="hi")
    with pytest.raises(ValueError):
        with t.llm("bad", "m"):
            raise ValueError("parse failed")
    t.close()
    out = tmp_path / "d.html"
    assert main(["dashboard", str(path), "--out", str(out)]) == 0
    text = out.read_text()
    assert "<svg" in text and "http://" not in text and "bad_output" in text


def test_dashboard_with_no_rows(tmp_path):
    from llmtrace.dashboard import render
    t, path = tracer(tmp_path)
    t.close()
    assert "No data" in render(path)
