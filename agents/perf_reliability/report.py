"""Render a PerfVerdict as a self-contained HTML report (no external assets).

Used by the CLI (``--html <path>``) to produce a presentable, offline demo
surface — open the file in any browser. Everything is inlined (CSS + data), so
it works with no server and no network. Theme-aware (light/dark).

All dynamic text (including LLM output) is HTML-escaped before it reaches the
page.
"""

from __future__ import annotations

import html

from agents.perf_reliability.models import PerfVerdict

_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1a1d23; --muted: #626975;
  --line: #e4e7ec; --accent: #3b5bdb; --accent-soft: #edf0fd;
  --low-bg:#e6f4ea; --low-ink:#1e7a3a; --med-bg:#fdf3e2; --med-ink:#9a6212;
  --high-bg:#fdeaea; --high-ink:#b42318; --save-bg:#eaf1fb; --save-ink:#1c4bb6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0f1115; --card:#171a21; --ink:#e8eaed; --muted:#9aa2ad;
    --line:#262b34; --accent:#8aa1ff; --accent-soft:#1b2140;
    --low-bg:#12301d; --low-ink:#6ee79a; --med-bg:#332612; --med-ink:#f0be6b;
    --high-bg:#3a1a1a; --high-ink:#ff9a90; --save-bg:#17233f; --save-ink:#9db8ff;
  }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }
header.hero { border-bottom:1px solid var(--line); padding-bottom:20px; margin-bottom:24px; }
.kicker { color:var(--accent); font-weight:700; letter-spacing:.06em; text-transform:uppercase; font-size:12px; }
h1 { margin:6px 0 4px; font-size:26px; }
.sub { color:var(--muted); font-size:14px; }
.tiles { display:flex; flex-wrap:wrap; gap:12px; margin:20px 0 8px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; min-width:140px; flex:1; }
.tile .n { font-size:22px; font-weight:700; }
.tile .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.chips { margin:10px 0 4px; }
.chip { display:inline-block; background:var(--accent-soft); color:var(--accent);
  border-radius:999px; padding:4px 12px; font-size:13px; font-weight:600; margin:0 6px 6px 0; }
.section-title { margin:28px 0 10px; font-size:14px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:18px 20px; margin-bottom:14px; }
.card-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
.rank { background:var(--ink); color:var(--bg); border-radius:8px; width:26px; height:26px;
  display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:13px; }
.loc { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:13px;
  color:var(--muted); }
.badge { border-radius:999px; padding:3px 10px; font-size:12px; font-weight:700; margin-left:auto; }
.badge + .badge { margin-left:8px; }
.b-low{background:var(--low-bg);color:var(--low-ink);} .b-med{background:var(--med-bg);color:var(--med-ink);}
.b-high{background:var(--high-bg);color:var(--high-ink);} .b-save{background:var(--save-bg);color:var(--save-ink);}
.issue { color:var(--muted); margin:6px 0 10px; }
.rec { margin:0; }
.rec b { color:var(--accent); }
pre.snip { background:var(--accent-soft); border-radius:8px; padding:10px 12px; overflow:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:13px; margin:0 0 10px; }
footer { color:var(--muted); font-size:12px; margin-top:28px; border-top:1px solid var(--line); padding-top:14px; }
.pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 10px; font-size:12px; }
"""

_BADGE_CLASS = {"low": "b-low", "medium": "b-med", "high": "b-high"}


def _fmt_runtime(minutes: float | None) -> str:
    if minutes is None:
        return "unknown"
    hours = minutes / 60.0
    if hours >= 24:
        return f"{hours:.0f} h (~{hours / 24:.1f} days)"
    return f"{hours:.1f} h"


def _esc(s: object) -> str:
    return html.escape(str(s))


def _finding_card(i: int, f) -> str:
    loc = f"{f.notebook}:{f.line}" if f.line is not None else f.notebook
    comp = f.implementation_complexity.value
    badge_cls = _BADGE_CLASS.get(comp, "b-med")
    save = _esc(f.estimated_saving) if f.estimated_saving else ""
    save_badge = f'<span class="badge b-save">{save}</span>' if save else ""
    snip = f'<pre class="snip">{_esc(f.snippet)}</pre>' if f.snippet else ""
    return f"""
    <div class="card">
      <div class="card-head">
        <span class="rank">{i}</span>
        <span class="loc">{_esc(loc)}</span>
        <span class="badge {badge_cls}">{_esc(comp)} effort</span>
        {save_badge}
      </div>
      <div class="issue">{_esc(f.issue)}</div>
      {snip}
      <p class="rec"><b>Fix:</b> {_esc(f.recommendation)}</p>
    </div>"""


def render_html(verdict: PerfVerdict) -> str:
    v = verdict
    chips = "".join(f'<span class="chip">{_esc(a)}</span>' for a in v.bottleneck_assets) or (
        '<span class="chip">none</span>'
    )
    cards = "".join(_finding_card(i, f) for i, f in enumerate(v.findings, start=1)) or (
        '<div class="card">No optimization opportunities found.</div>'
    )
    src = _esc(v.audit_metadata.signal_source)
    created = _esc(v.audit_metadata.created_at)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Reliability Report — {_esc(v.job_name)}</title>
<style>{_CSS}</style></head><body><div class="wrap">
  <header class="hero">
    <div class="kicker">UC3 · Predictive Infrastructure &amp; Code Reliability</div>
    <h1>{_esc(v.job_name)}</h1>
    <div class="sub">{_esc(v.summary)}</div>
  </header>

  <div class="tiles">
    <div class="tile"><div class="n">{_fmt_runtime(v.total_runtime_minutes)}</div><div class="l">Total runtime</div></div>
    <div class="tile"><div class="n">{len(v.findings)}</div><div class="l">Opportunities</div></div>
    <div class="tile"><div class="n">{v.analyzed_assets}</div><div class="l">Assets analyzed</div></div>
    <div class="tile"><div class="n">{v.confidence_score:.0%}</div><div class="l">Confidence</div></div>
  </div>

  <div class="section-title">Bottleneck assets</div>
  <div class="chips">{chips}</div>

  <div class="section-title">Recommendations (highest impact first)</div>
  {cards}

  <footer>
    Recommend-only — a human decides what to apply. Source: <span class="pill">{src}</span>
    &nbsp;·&nbsp; generated {created}
  </footer>
</div></body></html>"""
