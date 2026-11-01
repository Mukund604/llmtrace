"""Writes one self contained HTML dashboard from a traces file. It needs no server or network."""
import datetime
import html
import json
import sqlite3
import sys
from collections import defaultdict

from .analysis import drift, llm_rows, percentile, summary

CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--grid:#e4e3df;--s1:#2a78d6;--s2:#eb6834;--card:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--ink:#f0efec;--muted:#c3c2b7;--grid:#3a3a37;--s1:#3987e5;--s2:#d95926;--card:#222221}}
*{box-sizing:border-box}body{margin:0;padding:28px;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:20px}.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--grid);border-radius:8px;padding:14px 16px}.n{font-size:26px;font-weight:600}.l{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}h2{font-size:13px;margin:0 0 10px}svg{width:100%;height:auto;display:block}
.alert{border-left:3px solid var(--s2);padding:6px 10px;margin:6px 0}table{border-collapse:collapse;width:100%}td,th{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:left}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
"""


def line_chart(points, color, label, unit=""):
    """Inline SVG line chart with a native hover title on each point."""
    w, h, left, bottom = 520, 170, 46, 22
    if not points:
        return "<p>No data</p>"
    top = max(v for _, v in points) * 1.1 or 1
    def x(i): return left + i * (w - left - 8) / max(len(points) - 1, 1)
    def y(v): return h - bottom - v / top * (h - bottom - 8)
    grid = "".join(f'<line x1="{left}" x2="{w-8}" y1="{y(top*f):.1f}" y2="{y(top*f):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{left-6}" y="{y(top*f)+4:.1f}" text-anchor="end" font-size="10" fill="var(--muted)">{top*f:.3g}</text>' for f in (0, .5, 1))
    path = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))
    dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="7" fill="transparent"><title>{html.escape(t)}: {v:.4g}{unit}</title></circle>' for i, (t, v) in enumerate(points))
    labels = "".join(f'<text x="{x(i):.1f}" y="{h-6}" text-anchor="middle" font-size="10" fill="var(--muted)">{html.escape(points[i][0])}</text>' for i in (0, len(points) // 2, len(points) - 1))
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(label)}">{grid}<polyline points="{path}" fill="none" stroke="var(--{color})" stroke-width="2"/>{dots}{labels}</svg>'


def buckets(rows, hours=24):
    grouped = defaultdict(list)
    for r in rows:
        grouped[int(r["start"] // (hours * 3600))].append(r)
    fmt = lambda k: datetime.datetime.fromtimestamp(k * hours * 3600, datetime.timezone.utc).strftime("%b %d")
    return [(fmt(k), grouped[k]) for k in sorted(grouped)]


def render(path, note=""):
    rows = llm_rows(path)
    s = summary(rows)
    days = buckets(rows)
    alerts = []
    if len(days) >= 2:
        alerts = drift([r for _, g in days[:-1] for r in g], days[-1][1])
    tiles = [("Calls", f"{s['calls']:,}"), ("Total cost", f"${s['cost']:,.2f}"), ("p95 latency", f"{s['p95_seconds']:.2f} s"), ("Error rate", f"{s['error_rate']:.1%}")]
    tile_html = "".join(f'<div class="card"><div class="n">{v}</div><div class="l">{k}</div></div>' for k, v in tiles)
    latency = line_chart([(d, percentile([r["seconds"] for r in g], .95)) for d, g in days], "s1", "p95 latency by day", " s")
    cost = line_chart([(d, sum(r["cost"] or 0 for r in g)) for d, g in days], "s1", "Cost by day")
    errors = line_chart([(d, 100 * sum(r["status"] == "error" for r in g) / len(g)) for d, g in days], "s2", "Error rate by day", "%")
    tokens = line_chart([(d, percentile([r["output_tokens"] or 0 for r in g], .5)) for d, g in days], "s1", "Median output tokens by day")
    alert_html = "".join(f'<div class="alert"><b>{html.escape(a["metric"])}</b> changed: median {a["baseline_p50"]:.4g} to {a["current_p50"]:.4g}</div>' for a in alerts) or "<p>No drift in the latest day.</p>"
    table = "".join(f'<tr><td>{html.escape(str(k))}</td><td class="r">{v:,}</td></tr>' for k, v in sorted(s["errors"].items(), key=lambda kv: -kv[1])) or '<tr><td colspan="2">No errors</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>llmtrace</title><style>{CSS}</style></head><body>
<h1>llmtrace</h1><div class="sub">{html.escape(note)}</div><div class="tiles">{tile_html}</div>
<div class="grid"><div class="card"><h2>p95 latency by day (seconds)</h2>{latency}</div><div class="card"><h2>Cost by day (dollars)</h2>{cost}</div>
<div class="card"><h2>Error rate by day (%)</h2>{errors}</div><div class="card"><h2>Median output tokens by day</h2>{tokens}</div>
<div class="card"><h2>Drift, latest day against all earlier days</h2>{alert_html}</div><div class="card"><h2>Failures by type</h2><table><tr><th>Type</th><th class="r">Calls</th></tr>{table}</table></div></div></body></html>"""


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    open(argv[1], "w", encoding="utf-8").write(render(argv[0], *argv[2:3]))


if __name__ == "__main__":
    main()
