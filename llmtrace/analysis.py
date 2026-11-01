"""Reports and drift detection over recorded spans."""
import math
import sqlite3
from collections import Counter

METRICS = ("seconds", "cost", "output_tokens", "input_tokens", "output_chars")


def llm_rows(path, since=None, until=None):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    query, args = "SELECT * FROM spans WHERE kind='llm'", []
    if since is not None:
        query += " AND start >= ?"
        args.append(since)
    if until is not None:
        query += " AND start < ?"
        args.append(until)
    return [dict(r) for r in db.execute(query + " ORDER BY start", args)]


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summary(rows):
    latency = [r["seconds"] for r in rows]
    errors = [r for r in rows if r["status"] == "error"]
    return dict(calls=len(rows), cost=sum(r["cost"] or 0 for r in rows), p50_seconds=percentile(latency, .5),
                p95_seconds=percentile(latency, .95), error_rate=len(errors) / len(rows) if rows else 0.0,
                errors=dict(Counter(r["error"] for r in errors)),
                by_model=dict(Counter(r["model"] for r in rows)))


def ks_statistic(a, b):
    """Two sample Kolmogorov Smirnov statistic: largest gap between the two distributions."""
    a, b = sorted(a), sorted(b)
    i = j = 0
    gap = 0.0
    while i < len(a) and j < len(b):
        x = min(a[i], b[j])
        while i < len(a) and a[i] == x:
            i += 1
        while j < len(b) and b[j] == x:
            j += 1
        gap = max(gap, abs(i / len(a) - j / len(b)))
    return gap


def ks_threshold(n, m, alpha):
    """Asymptotic critical value for the two sample KS test."""
    return math.sqrt(-0.5 * math.log(alpha / 2)) * math.sqrt((n + m) / (n * m))


def drift(baseline, current, alpha=0.001, min_samples=30, min_effect=0.1):
    """Flags metrics whose distribution changed, plus the error rate.

    alpha is strict because this runs repeatedly on several metrics. min_effect ignores gaps
    that are statistically real but too small to act on, which large windows would always flag.
    """
    alerts = []
    for metric in METRICS:
        a = [r[metric] for r in baseline if r[metric] is not None and r["status"] == "ok"]
        b = [r[metric] for r in current if r[metric] is not None and r["status"] == "ok"]
        if len(a) < min_samples or len(b) < min_samples or len(set(a) | set(b)) < 2:
            continue
        statistic = ks_statistic(a, b)
        if statistic > max(ks_threshold(len(a), len(b), alpha), min_effect):
            alerts.append(dict(metric=metric, statistic=statistic, baseline_p50=percentile(a, .5), current_p50=percentile(b, .5)))
    if len(baseline) >= min_samples and len(current) >= min_samples:
        p1 = sum(r["status"] == "error" for r in baseline) / len(baseline)
        p2 = sum(r["status"] == "error" for r in current) / len(current)
        pooled = (p1 * len(baseline) + p2 * len(current)) / (len(baseline) + len(current))
        if 0 < pooled < 1:
            z = (p2 - p1) / math.sqrt(pooled * (1 - pooled) * (1 / len(baseline) + 1 / len(current)))
            # One sided: an improving error rate is not an incident.
            if z > 3.3 and p2 - p1 > 0.01:
                alerts.append(dict(metric="error_rate", statistic=z, baseline_p50=p1, current_p50=p2))
    return alerts
