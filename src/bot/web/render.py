"""HTML rendering for the dashboard.

Plain Python string building rather than a template engine. For four sections
on a single-user tool, a template dependency and its packaging problems buy
nothing, and keeping the markup next to the data that shapes it makes the
whole thing easier to follow.

Styling is the Mudhorn Capital identity from `brand/index.html`:
cold, mineral, one accent used sparingly. A trading dashboard is scanned rather
than read, so state is encoded in shape as well as number — severity stripes and
pills, so anything needing attention reads before the figures do.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from ..metrics import JournalReport, render_excursions, render_summary
from ..models import AccountSnapshot, StandDownState, Trade
from ..options import ExpiryAlert

STYLES = """
:root {
  --ink:#0B0E12; --graphite:#161B22; --slate:#29313C; --pewter:#7D8896;
  --bone:#E9ECEF; --patina:#4E8C7D; --amber:#C08A3E; --rust:#B3524A;
  --serif: ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans: ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  --mono: ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --ease: cubic-bezier(.22,1,.36,1);
}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--bone);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--patina);outline-offset:3px}
a{color:var(--bone);text-decoration-color:var(--slate);text-underline-offset:4px}
a:hover{text-decoration-color:var(--patina)}
.wrap{width:min(100% - 2rem,1100px);margin-inline:auto}

header.bar{border-bottom:1px solid var(--slate);position:sticky;top:0;
  background:rgba(11,14,18,.92);backdrop-filter:blur(8px);z-index:10}
header.bar .wrap{display:flex;align-items:center;gap:.875rem;
  padding:.875rem 0;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:.7rem;font-family:var(--serif);
  font-size:1.0625rem;letter-spacing:.04em}
.brand svg path{fill:var(--bone)}
.brand .thin{color:var(--pewter)}
nav{margin-left:auto;display:flex;gap:.25rem;flex-wrap:wrap}
nav a{font-family:var(--mono);font-size:.6875rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter);text-decoration:none;
  padding:.4rem .7rem;border:1px solid transparent;border-radius:2px;
  transition:color .2s,border-color .2s,background .2s}
nav a:hover{color:var(--bone);border-color:var(--slate);background:var(--graphite)}

section{padding:2.5rem 0;border-bottom:1px solid var(--slate)}
section:last-of-type{border-bottom:0}
.eyebrow{font-family:var(--mono);font-size:.6875rem;letter-spacing:.18em;
  text-transform:uppercase;color:var(--pewter);margin:0 0 1rem}
h1,h2,h3{font-family:var(--serif);font-weight:400;letter-spacing:-.01em;margin:0}
h2{font-size:1.5rem;margin-bottom:.25rem}
.note{font-size:.8125rem;color:var(--pewter)}

.banner{border:1px solid var(--slate);border-left-width:3px;border-radius:2px;
  padding:1rem 1.125rem;margin-bottom:1rem;background:var(--graphite)}
.banner.crit{border-left-color:var(--rust)}
.banner.warn{border-left-color:var(--amber)}
.banner.ok{border-left-color:var(--patina)}
.banner b{display:block;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:.35rem}
.banner.crit b{color:var(--rust)} .banner.warn b{color:var(--amber)}
.banner.ok b{color:var(--patina)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:1px;background:var(--slate);border:1px solid var(--slate);border-radius:2px}
.stat{background:var(--graphite);padding:1rem 1.125rem}
.stat span{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter)}
.stat b{display:block;font-size:1.375rem;font-weight:500;margin-top:.3rem;
  font-variant-numeric:tabular-nums}
.stat small{color:var(--pewter);font-size:.75rem;font-variant-numeric:tabular-nums}
.pos{color:var(--patina)} .neg{color:var(--rust)}

table{width:100%;border-collapse:collapse;font-size:.8125rem;
  font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;border:1px solid var(--slate);border-radius:2px;
  margin-top:1.25rem}
th{text-align:left;font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);font-weight:400;
  padding:.7rem .875rem;border-bottom:1px solid var(--slate);white-space:nowrap}
td{padding:.7rem .875rem;border-bottom:1px solid var(--slate);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr{transition:background .15s}
tbody tr:hover{background:var(--graphite)}
.pill{display:inline-block;font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.1em;text-transform:uppercase;padding:.15rem .45rem;
  border:1px solid currentColor;border-radius:2px}
.rationale{color:var(--pewter);max-width:38ch}

.curve{margin-top:1.25rem;border:1px solid var(--slate);border-radius:2px;
  background:var(--graphite);padding:1.25rem}
.curve svg{display:block;width:100%;height:auto}

.readout{font-family:var(--mono);font-size:.75rem;color:var(--pewter);
  background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1rem 1.125rem;margin-top:1.25rem;white-space:pre-wrap;line-height:1.7}

.empty{color:var(--pewter);font-style:italic;padding:1.5rem 0}
footer{padding:2rem 0 3rem;color:var(--pewter);font-size:.75rem}

.reveal{opacity:0;transform:translateY(14px);
  transition:opacity .6s var(--ease),transform .6s var(--ease)}
.reveal.in{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){
  .reveal{opacity:1!important;transform:none!important;transition:none}
  html{scroll-behavior:auto}
}
"""

MARK = (
    '<svg viewBox="0 0 64 64" width="24" height="24" aria-hidden="true">'
    '<path fill-rule="evenodd" d="M32 2a30 30 0 1 0 0 60 30 30 0 0 0 0-60Z'
    'm0 8a22 22 0 1 1 0 44 22 22 0 0 1 0-44Z"/>'
    '<path d="M32 17 47 45h-9.4L32 34.2 26.4 45H17L32 17Z"/></svg>'
)

SCRIPT = """
const io = new IntersectionObserver((es) => {
  for (const e of es) if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
}, { rootMargin: '0px 0px -8% 0px' });
document.querySelectorAll('.reveal').forEach((el) => io.observe(el));
"""


def _e(value: object) -> str:
    return html.escape(str(value))


def _money(value: float | None, *, sign: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if sign and value > 0 else ''}${value:,.2f}"


def _cls(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} — Mudhorn Capital</title><style>{STYLES}</style></head><body>
<header class="bar"><div class="wrap">
  <span class="brand">{MARK} MUDHORN <span class="thin">CAPITAL</span></span>
  <nav>
    <a href="#overview">Overview</a><a href="#analytics">Analytics</a>
    <a href="#trades">Trades</a><a href="#rules">Rules</a>
    <a href="#chat">Chat</a>
  </nav>
</div></header>
<main>{body}</main>
<footer class="wrap">Paper trading. Private vehicle, not managing anyone
else's money. Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC.</footer>
<script>{SCRIPT}</script></body></html>"""


def banners(stand_down: StandDownState, alerts: list[ExpiryAlert], account: AccountSnapshot,
            untracked: list[str]) -> str:
    """Anything needing attention, ahead of the numbers.

    Ordered by consequence rather than by section: an expiry resolves itself
    automatically and irreversibly, so it leads.
    """
    out: list[str] = []
    now = datetime.now(UTC)

    for alert in (a for a in alerts if a.needs_action):
        out.append(
            f'<div class="banner crit"><b>Option expiry, action required</b>'
            f'{_e(alert.message)}</div>'
        )

    if stand_down.is_active(now):
        out.append(
            f'<div class="banner warn"><b>Stage {stand_down.stage} stand-down</b>'
            f'{stand_down.consecutive_losses} consecutive losses. Live trading is '
            f'suspended for {stand_down.days_remaining(now):.1f} more days. '
            f'Paper trading continues as normal.</div>'
        )

    if untracked:
        out.append(
            f'<div class="banner warn"><b>Open risk is understated</b>'
            f'{len(untracked)} held position(s) have no journal entry '
            f'({_e(", ".join(untracked))}), so their planned stop is unknown and '
            f'their risk is not counted below. Actual risk is higher than shown.</div>'
        )

    if not out:
        out.append('<div class="banner ok"><b>Clear</b>No stand-down, no expiries '
                   'due, and every open position is accounted for.</div>')
    return "".join(out)


def overview(account: AccountSnapshot, report: JournalReport,
             curve: list[tuple[str, float]], max_risk_pct: float) -> str:
    risk_pct = (account.open_risk_usd / account.equity_usd * 100) if account.equity_usd else 0.0
    pnl = report.overall.total_pnl_usd

    return f"""<section id="overview" class="wrap reveal">
<p class="eyebrow">Overview</p>
<h2>Account</h2>
<div class="stats" style="margin-top:1.25rem">
  <div class="stat"><span>Equity</span><b>{_money(account.equity_usd)}</b></div>
  <div class="stat"><span>Cash</span><b>{_money(account.cash_usd)}</b>
    <small>{account.cash_pct:.1f}% of equity</small></div>
  <div class="stat"><span>Open risk</span><b>{_money(account.open_risk_usd)}</b>
    <small>{risk_pct:.2f}% of {max_risk_pct:.1f}% cap</small></div>
  <div class="stat"><span>Open positions</span><b>{len(account.open_positions)}</b></div>
  <div class="stat"><span>Realised P&amp;L</span>
    <b class="{_cls(pnl)}">{_money(pnl, sign=True)}</b>
    <small>{report.overall.trade_count} closed trades</small></div>
</div>
{_curve(curve)}
{_positions(account)}
</section>"""


def _curve(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return ('<p class="note" style="margin-top:1.25rem">Equity curve appears '
                'once there are at least two daily snapshots.</p>')

    # A real aspect ratio with uniform scaling, rather than stretching a square
    # viewBox to fit. Stretching turns the endpoint marker into an ellipse and
    # clips it at the edge, which is exactly the sort of detail that makes a
    # chart look unconsidered.
    w, h, pad = 600.0, 150.0, 10.0
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    inner_w, inner_h = w - pad * 2, h - pad * 2
    step = inner_w / (len(values) - 1)
    coords = [
        (pad + i * step, pad + inner_h - ((v - lo) / span * inner_h))
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{pad:.1f},{h - pad:.1f} {line} {w - pad:.1f},{h - pad:.1f}"
    last_x, last_y = coords[-1]

    return f"""<div class="curve">
<svg viewBox="0 0 {w:.0f} {h:.0f}" role="img"
     aria-label="Equity curve, {points[0][0]} to {points[-1][0]}, low {_money(lo)}, high {_money(hi)}">
  <line x1="{pad}" y1="{h - pad:.1f}" x2="{w - pad:.1f}" y2="{h - pad:.1f}"
        stroke="#29313C" stroke-width="1"/>
  <polygon points="{area}" fill="#4E8C7D" opacity=".12"/>
  <polyline points="{line}" fill="none" stroke="#4E8C7D" stroke-width="1.75"
            stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="#0B0E12"
          stroke="#4E8C7D" stroke-width="2"/>
</svg>
<p class="note" style="margin:.75rem 0 0">Equity {points[0][0]} to {points[-1][0]},
  low {_money(lo)}, high {_money(hi)}.</p>
</div>"""


def _positions(account: AccountSnapshot) -> str:
    if not account.open_positions:
        return '<p class="empty">No open positions.</p>'
    rows = "".join(
        f"<tr><td><b>{_e(p.symbol)}</b></td>"
        f'<td><span class="pill">{_e(p.direction.value)}</span></td>'
        f"<td>{p.qty:g}</td><td>{_money(p.entry_price)}</td>"
        f"<td>{_money(p.current_price) if p.current_price else 'n/a'}</td>"
        f'<td class="{_cls(p.unrealised_pnl_usd)}">'
        f"{_money(p.unrealised_pnl_usd, sign=True)}</td></tr>"
        for p in account.open_positions
    )
    return f"""<div class="scroll"><table>
<thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th>
<th>Now</th><th>Unrealised</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def analytics(report: JournalReport) -> str:
    s = report.overall
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
    exp_r = f"{s.expectancy_r:+.2f}R" if s.expectancy_r is not None else "n/a"
    readout = "\n".join(
        [*render_summary(s), "", *render_excursions(report.excursions)]
    )

    by_strategy = "".join(
        f"<tr><td>{_e(name)}</td><td>{g.trade_count}</td>"
        f"<td>{g.win_rate:.0%}</td>"
        f'<td class="{_cls(g.total_pnl_usd)}">{_money(g.total_pnl_usd, sign=True)}</td></tr>'
        for name, g in report.by_strategy.items()
    )
    strategy_table = (
        f'<div class="scroll"><table><thead><tr><th>Strategy</th><th>Trades</th>'
        f"<th>Win rate</th><th>P&amp;L</th></tr></thead><tbody>{by_strategy}"
        f"</tbody></table></div>"
        if by_strategy else ""
    )

    return f"""<section id="analytics" class="wrap reveal">
<p class="eyebrow">Analytics</p>
<h2>Performance</h2>
<p class="note">{_e(s.health)}</p>
<div class="stats" style="margin-top:1.25rem">
  <div class="stat"><span>Win rate</span><b>{s.win_rate:.0%}</b>
    <small>{s.wins}W / {s.losses}L</small></div>
  <div class="stat"><span>Profit factor</span><b>{pf}</b>
    <small>&gt;1.5 solid, &gt;2 strong</small></div>
  <div class="stat"><span>Expectancy</span>
    <b class="{_cls(s.expectancy_usd)}">{_money(s.expectancy_usd, sign=True)}</b>
    <small>{exp_r} per trade</small></div>
  <div class="stat"><span>Max drawdown</span><b>{_money(s.max_drawdown_usd)}</b></div>
</div>
{strategy_table}
<div class="readout">{_e(readout)}</div>
</section>"""


def trades(recent: list[Trade]) -> str:
    if not recent:
        return """<section id="trades" class="wrap reveal">
<p class="eyebrow">Trades</p><h2>Closed trades</h2>
<p class="empty">No closed trades yet.</p></section>"""

    rows = ""
    for t in reversed(recent):
        r = f"{t.r_multiple:+.2f}R" if t.r_multiple is not None else "n/a"
        rows += (
            f"<tr><td>{_e(t.exit_time.date().isoformat() if t.exit_time else '')}</td>"
            f"<td><b>{_e(t.symbol)}</b></td>"
            f'<td><span class="pill">{_e(t.strategy)}</span></td>'
            f'<td class="{_cls(t.net_pnl_usd)}">{_money(t.net_pnl_usd, sign=True)}</td>'
            f'<td class="{_cls(t.r_multiple)}">{_e(r)}</td>'
            f"<td>{_money(t.mae_usd)}</td><td>{_money(t.mfe_usd)}</td>"
            f'<td class="rationale">{_e(t.rationale)}</td></tr>'
        )

    return f"""<section id="trades" class="wrap reveal">
<p class="eyebrow">Trades</p>
<h2>Closed trades</h2>
<p class="note">Newest first. MAE and MFE are sampled once per decision cycle,
  so both understate the true swing.</p>
<div class="scroll"><table>
<thead><tr><th>Closed</th><th>Symbol</th><th>Strategy</th><th>Net</th><th>R</th>
<th>MAE</th><th>MFE</th><th>Rationale</th></tr></thead>
<tbody>{rows}</tbody></table></div>
</section>"""


def rules_view(rules_yaml: str) -> str:
    """Rules are shown, not edited.

    Editing limits from a web form is the highest-risk feature in the project,
    and for one operator "open the file" is a perfectly good answer. Changing a
    limit should also be deliberate enough to leave a commit behind.
    """
    return f"""<section id="rules" class="wrap reveal">
<p class="eyebrow">Rules</p>
<h2>Active limits</h2>
<p class="note">Read-only. Edit <code>config/rules.yaml</code> and restart to
  change behaviour, so every change leaves a commit behind rather than happening
  quietly from a phone after a bad day.</p>
<div class="readout">{_e(rules_yaml)}</div>
</section>"""


def chat_panel(*, enabled: bool, token: str, hermes_available: bool) -> str:
    """The one interactive thing on an otherwise read-only page.

    Kept last, below the rules, because it is the part that acts and everything
    above it merely reports. If it is switched off — the default — this renders
    an explanation rather than nothing, so a blank space is never mistaken for a
    broken feature.
    """
    if not enabled:
        return """<section id="chat" class="wrap reveal">
<p class="eyebrow">Chat</p>
<h2>Ask the agent</h2>
<p class="note">Disabled. Set <code>DASHBOARD_CHAT_TOKEN</code> in
  <code>.env</code> and restart to switch it on. It is off by default because
  everything else on this page reports, and this one acts.</p>
</section>"""

    if not hermes_available:
        return """<section id="chat" class="wrap reveal">
<p class="eyebrow">Chat</p>
<h2>Ask the agent</h2>
<p class="note">Enabled, but Hermes is not installed on this machine. See
  <code>docs/HERMES_SETUP.md</code>.</p>
</section>"""

    return f"""<section id="chat" class="wrap reveal">
<p class="eyebrow">Chat</p>
<h2>Ask the agent</h2>
<p class="note">Hermes answers, with the same tools and the same approval rules
  as anywhere else. Every order-placing tool still runs the risk gate first, so
  nothing here can talk its way past <code>config/rules.yaml</code>.</p>
<div id="chat-log" class="readout" style="min-height:6rem"></div>
<div style="display:flex;gap:.5rem;margin-top:1rem">
  <input id="chat-input" type="text" placeholder="What is my risk status?"
         style="flex:1;padding:.6rem .8rem;background:var(--ink);color:var(--bone);
                border:1px solid var(--slate);border-radius:2px;font:inherit">
  <button id="chat-send" class="replay" style="margin-top:0">Send</button>
</div>
<script>
(function () {{
  const log = document.getElementById('chat-log');
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  const history = [];

  function line(who, text) {{
    const p = document.createElement('p');
    p.style.margin = '0 0 .6rem';
    const b = document.createElement('b');
    b.textContent = who + ': ';
    p.appendChild(b);
    // textContent, never innerHTML: the reply is model output.
    p.appendChild(document.createTextNode(text));
    log.appendChild(p);
    log.scrollTop = log.scrollHeight;
  }}

  async function ask() {{
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    line('You', message);
    send.disabled = true;
    line('Agent', 'thinking...');
    const pending = log.lastChild;
    try {{
      const r = await fetch('/chat', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: {token!r}, message: message, history: history}})
      }});
      const data = await r.json();
      log.removeChild(pending);
      if (data.ok) {{
        line('Agent', data.text);
        history.push({{user: message, agent: data.text}});
      }} else {{
        line('Error', data.error || 'request failed');
      }}
    }} catch (e) {{
      log.removeChild(pending);
      line('Error', String(e));
    }} finally {{
      send.disabled = false;
      input.focus();
    }}
  }}

  send.addEventListener('click', ask);
  input.addEventListener('keydown', function (e) {{ if (e.key === 'Enter') ask(); }});
}})();
</script>
</section>"""
