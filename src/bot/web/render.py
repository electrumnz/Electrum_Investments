"""HTML rendering for the operator command centre.

Plain Python string building rather than a template engine. For a single-user
tool, a template dependency and its packaging problems buy nothing, and keeping
the markup next to the data that shapes it makes the whole thing easier to
follow.

Styling is the Mudhorn Capital identity: cold, mineral, one accent used
sparingly, every figure tabular. A trading surface is scanned rather than read,
so state is encoded in shape as well as in number and anything needing
attention reads before the figures do.

## This is the real one

`brand/` is the public site and every number on it is invented. Everything here
is live: the journal, the broker, the audit log. That is the whole reason this
binds to `127.0.0.1` and is reached over Tailscale rather than published.

Read-only apart from `POST /chat`. Nothing rendered here can place, close or
resize a position, and the Settings page shows the risk limits without offering
a way to change them: limits move in a commit, by a person, with a reason.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime

from ..audit import AuditView, DecisionEntry
from ..config import DAY_NAMES, Env, Rules
from ..metrics import JournalReport, render_excursions, render_summary
from ..models import AccountSnapshot, StandDownState, Trade, WorkingOrder
from ..options import ExpiryAlert
from ..tailnet import TailnetStatus

STYLES = """
:root {
  --ink:#0B0E12; --graphite:#161B22; --slate:#29313C; --pewter:#7D8896;
  --bone:#E9ECEF; --patina:#4E8C7D;
  /* Severity, for banners. */
  --amber:#C08A3E; --rust:#B3524A;
  /* Figures only, lifted for contrast on graphite. Same pair as the public
     site: patina brightened for a gain, a cold rose for a loss, because a warm
     red on this ground reads as brick. */
  --gain:#5FA795; --loss:#C0707B;
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
.wrap{width:min(100% - 2rem,1240px);margin-inline:auto}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pos,.gain{color:var(--gain)} .neg,.loss{color:var(--loss)} .muted{color:var(--pewter)}
.note{font-size:.8125rem;color:var(--pewter)}
h1,h2,h3{font-family:var(--serif);font-weight:400;letter-spacing:-.01em;margin:0}
h1{font-size:1.75rem} h2{font-size:1.375rem;margin-bottom:.25rem} h3{font-size:1rem}
.eyebrow{font-family:var(--mono);font-size:.6875rem;letter-spacing:.18em;
  text-transform:uppercase;color:var(--pewter);margin:0}

header.bar{border-bottom:1px solid var(--slate);position:sticky;top:0;
  background:rgba(11,14,18,.94);backdrop-filter:blur(8px);z-index:20}
header.bar .wrap{display:flex;align-items:center;gap:.875rem;
  padding:.75rem 0;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:.6rem;font-family:var(--serif);
  font-size:1rem;letter-spacing:.04em;text-decoration:none;white-space:nowrap}
.brand svg path{fill:var(--bone)}
.brand .thin{color:var(--pewter)}
nav{margin-left:auto;display:flex;gap:.2rem;flex-wrap:wrap}
nav a{font-family:var(--mono);font-size:.6875rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter);text-decoration:none;
  padding:.4rem .7rem;border:1px solid transparent;border-radius:2px;
  transition:color .2s,border-color .2s,background .2s}
nav a:hover{color:var(--bone);border-color:var(--slate);background:var(--graphite)}
nav a[aria-current=page]{color:var(--bone);border-color:var(--patina)}
.live{font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);display:flex;align-items:center;
  gap:.4rem;padding-left:.875rem;border-left:1px solid var(--slate)}
.live i{width:6px;height:6px;border-radius:50%;background:var(--patina);
  display:inline-block}
.live.paper i{background:var(--patina)}

main{padding:2rem 0 4rem}
.page-head{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
  margin-bottom:1.5rem}
.page-head .asof{margin-left:auto;font-family:var(--mono);font-size:.75rem;
  color:var(--pewter)}
section.block{margin-top:2.5rem}
section.block>h2{margin-bottom:.75rem}

.banner{border:1px solid var(--slate);border-left-width:3px;border-radius:2px;
  padding:.875rem 1.125rem;margin-bottom:.75rem;background:var(--graphite);
  font-size:.875rem}
.banner.crit{border-left-color:var(--rust)}
.banner.warn{border-left-color:var(--amber)}
.banner.ok{border-left-color:var(--patina)}
.banner b{display:block;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:.3rem}
.banner.crit b{color:var(--rust)} .banner.warn b{color:var(--amber)}
.banner.ok b{color:var(--patina)}

.grid{display:grid;gap:1rem;align-items:start}
.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1.125rem}
.card h3{margin-bottom:.5rem}

.stat span.k{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter)}
.stat b{display:block;font-size:1.5rem;font-weight:500;margin-top:.35rem;
  font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.stat small{display:block;color:var(--pewter);font-size:.75rem;margin-top:.3rem}

.meter{margin-top:.75rem}
.meter .track{height:6px;background:var(--ink);border:1px solid var(--slate);
  border-radius:2px;overflow:hidden}
.meter .fill{height:100%;background:var(--patina)}
.meter .fill.over{background:var(--loss)}
.meter .legend{display:flex;justify-content:space-between;margin-top:.45rem;
  font-family:var(--mono);font-size:.6875rem;color:var(--pewter)}
.pips{display:flex;gap:.35rem;margin-top:.6rem}
.pips i{width:26px;height:6px;border:1px solid var(--slate);border-radius:2px;
  background:var(--ink)}
.pips i.lit{background:var(--loss);border-color:var(--loss)}

table{width:100%;border-collapse:collapse;font-size:.8125rem}
.scroll{overflow-x:auto;border:1px solid var(--slate);border-radius:2px}
caption{text-align:left;padding:0 0 .6rem;color:var(--pewter);font-size:.8125rem}
th{text-align:left;font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);font-weight:400;
  padding:.7rem .875rem;border-bottom:1px solid var(--slate);white-space:nowrap;
  background:var(--graphite);position:sticky;top:0}
td{padding:.7rem .875rem;border-bottom:1px solid var(--slate);vertical-align:top}
td.r,th.r{text-align:right}
tr.data td.r{white-space:nowrap}
tr:last-child td{border-bottom:0}
tr.data td{border-bottom:0}
tr.why td{padding-top:0;padding-bottom:.8rem;border-bottom:1px solid var(--slate);
  max-width:0}
tr.why .quote{border-left:2px solid var(--patina);padding-left:.875rem;
  font-family:var(--serif);font-size:.9375rem;color:var(--bone)}
.pill{display:inline-block;font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.1em;text-transform:uppercase;padding:.15rem .45rem;
  border:1px solid currentColor;border-radius:2px;white-space:nowrap}
.pill.win{color:var(--gain)} .pill.loss{color:var(--loss)}
.pill.ok{color:var(--patina)} .pill.no{color:var(--rust)}
.pill.hold{color:var(--pewter)}

.curve{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  padding:1.125rem}
.curve svg{display:block;width:100%;height:auto}
.curve .line{fill:none;stroke:var(--patina);stroke-width:1.75;
  stroke-linejoin:round;stroke-linecap:round}
.curve .area{fill:color-mix(in srgb,var(--patina) 14%,transparent);stroke:none}
.curve .base{stroke:var(--slate);stroke-width:1;stroke-dasharray:3 4}
.curve .tick{fill:var(--pewter);font-family:var(--mono);font-size:10px}

.readout{font-family:var(--mono);font-size:.75rem;color:var(--pewter);
  background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1rem 1.125rem;white-space:pre-wrap;line-height:1.7}
.empty{color:var(--pewter);font-style:italic;padding:1.25rem}

/* ------------------------------------------------------------- decisions */
.cycle{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  margin-bottom:1rem}
.cycle>.head{display:flex;gap:.875rem;align-items:baseline;flex-wrap:wrap;
  padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
.cycle>.head .when{font-family:var(--mono);font-size:.8125rem}
.cycle>.head .cost{margin-left:auto;font-family:var(--mono);font-size:.6875rem;
  color:var(--pewter)}
.cycle .assessment{padding:.875rem 1.125rem;color:var(--pewter);
  font-size:.875rem;border-bottom:1px solid var(--slate)}
.cycle .assessment q{font-family:var(--serif);font-size:.9375rem;color:var(--bone)}
.step{padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
.step:last-child{border-bottom:0}
.step .what{display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap}
.step .what b{font-family:var(--mono);font-size:.875rem}
.chain{display:grid;gap:0;margin-top:.7rem;border-left:2px solid var(--slate);
  padding-left:.875rem}
.chain .rung{padding:.3rem 0;font-size:.8125rem}
.chain .rung .lbl{font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);margin-right:.5rem}
.chain .rung.gate.no{color:var(--loss)}
.chain .rung.gate.ok{color:var(--gain)}
.reasons{margin:.35rem 0 0;padding-left:1.1rem;color:var(--loss);font-size:.8125rem}
.reasons li{margin:.15rem 0}
.pill.watch{color:var(--amber)}
.considered{padding:.5rem 0;border-bottom:1px dashed var(--slate)}
.considered:last-child{border-bottom:0}
.considered .chain{margin-top:.4rem;border-left-color:var(--slate)}
.feed{margin:.3rem 0 0;padding-left:1.1rem;font-size:.8125rem;color:var(--pewter)}
.feed li{margin:.15rem 0}
details.step summary{cursor:pointer;list-style:none}
details.step summary::-webkit-details-marker{display:none}
/* Literal characters, never CSS hex escapes. STYLES is an ordinary Python
   string, so a CSS escape like backslash-2-5-B-8 is read by PYTHON first,
   as an OCTAL escape, and the stylesheet receives a control character. The
   browser then draws a tofu box beside the leftover digits. Nothing warns.
   Keep backslashes out of this string altogether. */
details.step summary::before{content:"▸ ";color:var(--pewter)}
details.step[open] summary::before{content:"▾ "}
details.step summary:hover{color:var(--bone)}

/* ------------------------------------------------------------------ chat */
.chat{display:flex;flex-direction:column;gap:1rem}
.log{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  padding:1.125rem;min-height:340px;max-height:60vh;overflow-y:auto;
  display:flex;flex-direction:column;gap:1rem}
.turn{display:flex;flex-direction:column;gap:.3rem}
.turn .who{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter)}
.turn .msg{white-space:pre-wrap;font-size:.9375rem}
.turn.agent .msg{border-left:2px solid var(--patina);padding-left:.875rem}
.turn.err .msg{border-left:2px solid var(--rust);padding-left:.875rem;
  color:var(--loss);font-family:var(--mono);font-size:.8125rem}
.composer{display:flex;gap:.6rem;align-items:flex-end}
textarea,input[type=text]{flex:1;background:var(--graphite);color:var(--bone);
  border:1px solid var(--slate);border-radius:2px;padding:.7rem .875rem;
  font-family:var(--sans);font-size:.9375rem;resize:vertical;min-height:44px}
textarea:hover,input:hover{border-color:var(--pewter)}
button.btn{background:transparent;color:var(--pewter);border:1px solid var(--slate);
  border-radius:2px;padding:.7rem 1.125rem;font-family:var(--mono);
  font-size:.6875rem;letter-spacing:.16em;text-transform:uppercase;cursor:pointer;
  transition:color .2s,border-color .2s,background .2s;min-height:44px}
button.btn:hover:not(:disabled){color:var(--bone);border-color:var(--patina);
  background:var(--graphite)}
button.btn:disabled{opacity:.5;cursor:not-allowed}
.prompts{display:flex;gap:.4rem;flex-wrap:wrap}
.prompts button{font-size:.6875rem;padding:.4rem .7rem;min-height:0;
  text-transform:none;letter-spacing:.02em;font-family:var(--sans);
  color:var(--pewter);background:transparent;border:1px solid var(--slate);
  border-radius:2px;cursor:pointer}
.prompts button:hover{color:var(--bone);border-color:var(--patina)}

/* --------------------------------------------------------------- settings */
.kv{display:grid;grid-template-columns:minmax(140px,auto) 1fr;gap:.4rem .875rem;
  font-size:.875rem}
.kv dt{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--pewter);padding-top:.15rem}
.kv dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums}
.kv dd .why{display:block;font-family:var(--sans);font-size:.8125rem;
  color:var(--pewter);margin-top:.1rem}
td.thin{color:var(--pewter);font-style:italic}
.source{margin-top:.875rem;font-size:.75rem;color:var(--pewter);
  border-top:1px solid var(--slate);padding-top:.6rem}
.source code{font-family:var(--mono);color:var(--bone)}

footer{padding:2rem 0 3rem;color:var(--pewter);font-size:.75rem;
  border-top:1px solid var(--slate)}

@media (max-width:760px){
  nav{margin-left:0;width:100%;order:3}
  .live{margin-left:auto;padding-left:0;border-left:0}
  .scroll{border:0}
  table,thead,tbody,tr,td{display:block;width:100%}
  caption{display:block}
  thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  tr.data{border:1px solid var(--slate);border-bottom:0;background:var(--graphite);
    margin-top:.875rem;padding:.4rem 0}
  tr.why{border:1px solid var(--slate);border-top:0;background:var(--graphite)}
  tr.data td,tr.why td{border-bottom:0;padding:.3rem .875rem;max-width:none;
    display:flex;gap:1rem;justify-content:space-between;align-items:baseline}
  tr.why td{display:block;padding-bottom:.8rem}
  td[data-l]::before{content:attr(data-l);font-family:var(--mono);font-size:.625rem;
    letter-spacing:.12em;text-transform:uppercase;color:var(--pewter);flex:none}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

MARK = (
    '<svg viewBox="0 0 64 64" width="22" height="22" aria-hidden="true">'
    '<path fill-rule="evenodd" d="M32 2a30 30 0 1 0 0 60 30 30 0 0 0 0-60Z'
    'm0 8a22 22 0 1 1 0 44 22 22 0 0 1 0-44Z"/>'
    '<path d="M32 17 47 45h-9.4L32 34.2 26.4 45H17L32 17Z"/></svg>'
)

# Inlined as a data URI rather than served from a route. A browser asks for
# /favicon.ico on every fresh page load, and without this the dashboard answers
# 404 into the console on a surface whose whole job is making problems visible.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' fill='%230B0E12'/%3E"
    "%3Cg fill='%23E9ECEF'%3E%3Cpath fill-rule='evenodd' d='M32 6a26 26 0 1 0 0 52"
    " 26 26 0 0 0 0-52Zm0 7a19 19 0 1 1 0 38 19 19 0 0 1 0-38Z'/%3E"
    "%3Cpath d='M32 19 45 44h-8.1L32 35.3 27.1 44H19L32 19Z'/%3E%3C/g%3E%3C/svg%3E"
)

PAGES: tuple[tuple[str, str], ...] = (
    ("/", "Board"),
    ("/decisions", "Decisions"),
    ("/trades", "Trades"),
    ("/analytics", "Analytics"),
    ("/settings", "Settings"),
    ("/chat", "Chat"),
)


# ------------------------------------------------------------------ helpers


def _e(value: object) -> str:
    return html.escape(str(value))


def _money(value: float | None, *, sign: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if sign and value > 0 else ''}${value:,.2f}"


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}%"


def _cls(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _when(stamp: datetime) -> str:
    return stamp.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def stat(label: str, value: str, meta: str = "", cls: str = "") -> str:
    return (
        f'<div class="card stat"><span class="k">{_e(label)}</span>'
        f'<b class="{cls}">{value}</b>'
        + (f"<small>{meta}</small>" if meta else "")
        + "</div>"
    )


def meter(used: float, cap: float, unit: str, digits: int = 2) -> str:
    ratio = min(used / cap, 1.0) if cap else 0.0
    over = "over" if cap and used > cap else ""
    return (
        f'<div class="meter"><div class="track">'
        f'<div class="fill {over}" style="width:{ratio * 100:.1f}%"></div></div>'
        f'<div class="legend"><span>{used:.{digits}f}{unit} used</span>'
        f"<span>{cap:.{digits}f}{unit} cap</span></div></div>"
    )


def pips(lit: int, total: int) -> str:
    cells = "".join(
        f'<i class="{"lit" if i < lit else ""}"></i>' for i in range(total)
    )
    return (
        f'<div class="pips" role="img" aria-label="{lit} of {total} qualifying '
        f'losses toward the stand-down trigger">{cells}</div>'
    )


def shell(
    title: str, active: str, body: str, *, env: Env, exposed: bool = False
) -> str:
    nav = "".join(
        f'<a href="{path}"{" aria-current=page" if path == active else ""}>{label}</a>'
        for path, label in PAGES
    )
    if exposed:
        nav += '<a href="/logout">Sign out</a>'
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>{_e(title)} &middot; Mudhorn Capital</title><style>{STYLES}</style></head><body>
<header class="bar"><div class="wrap">
  <a class="brand" href="/">{MARK} MUDHORN <span class="thin">CAPITAL</span></a>
  <nav>{nav}</nav>
  <span class="live paper"><i></i>{_e(mode)}</span>
</div></header>
<main><div class="wrap">{body}</div></main>
<footer class="wrap">Live operator view{
    " behind a shared password" if exposed else ", bound to the loopback interface"
}. Paper trading. Private vehicle, not managing anyone else's money. Rendered
{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC.</footer>
</body></html>"""


def head(eyebrow: str, title: str, asof: str = "", lede: str = "") -> str:
    return (
        f'<div class="page-head"><div><p class="eyebrow">{_e(eyebrow)}</p>'
        f"<h1>{_e(title)}</h1></div>"
        + (f'<p class="asof">{_e(asof)}</p>' if asof else "")
        + "</div>"
        + (f'<p class="note" style="max-width:68ch">{_e(lede)}</p>' if lede else "")
    )


# ------------------------------------------------------------------ banners


def banners(
    stand_down: StandDownState,
    alerts: list[ExpiryAlert],
    untracked: list[str],
    *,
    stale: list[str] | None = None,
    audit: AuditView | None = None,
    tailnet: TailnetStatus | None = None,
) -> str:
    """Anything needing attention, ahead of the numbers.

    Ordered by consequence rather than by section: an option expiry resolves
    itself automatically and irreversibly if nobody is watching, so it leads.
    """
    out: list[str] = []
    now = datetime.now(UTC)

    # Deliberately on the surface that is about to disappear. It reads backwards
    # — a warning about losing the dashboard, shown on the dashboard — and it is
    # the only channel that works: the failure is 80 days of notice followed by
    # an outage, and during the notice period this page is up and being looked
    # at. After the key lapses nothing on this box can reach anyone.
    if tailnet is not None and tailnet.needs_attention(now=now):
        out.append(
            '<div class="banner warn"><b>Tailscale link</b>'
            f"{_e(tailnet.headline(now=now))}</div>"
        )

    for alert in (a for a in alerts if a.needs_action):
        out.append(
            '<div class="banner crit"><b>Option expiry, action required</b>'
            f"{_e(alert.message)}</div>"
        )

    if stand_down.is_active(now):
        out.append(
            f'<div class="banner warn"><b>Stage {stand_down.stage} stand-down</b>'
            f"{stand_down.consecutive_losses} consecutive losses. Live trading is "
            f"suspended for {stand_down.days_remaining(now):.1f} more days. "
            "Paper trading continues as normal, and closing a position or moving "
            "a stop is never blocked.</div>"
        )

    if untracked:
        out.append(
            '<div class="banner warn"><b>Open risk is understated</b>'
            f"{len(untracked)} held position(s) have no journal entry "
            f"({_e(', '.join(untracked))}), so their planned stop is unknown and "
            "their risk is not counted below. Actual risk is higher than shown.</div>"
        )

    # The mirror image of `untracked`, and the one that is easy to miss: the
    # journal says a position is open, the broker does not hold it. Open risk
    # counts from the journal, so this makes the headline figure OVERSTATED
    # rather than understated, and the Board would otherwise show risk against
    # an empty position list with nothing to explain the contradiction.
    if stale:
        out.append(
            '<div class="banner warn"><b>Open risk may be overstated</b>'
            f"The journal holds {len(stale)} open trade(s) the broker is not "
            f"reporting ({_e(', '.join(stale))}). Either they closed outside the "
            "bot, or reconciliation has not run since they did. Open risk below "
            "still counts them, so the real figure is lower.</div>"
        )

    if audit is not None and audit.is_degraded:
        detail = f"{audit.malformed} unreadable line(s)"
        if audit.unreadable_files:
            detail += f", {len(audit.unreadable_files)} unreadable file(s)"
        out.append(
            '<div class="banner warn"><b>Decision log is incomplete</b>'
            f"{detail}. The trail below is missing entries rather than showing "
            "everything that happened.</div>"
        )

    if not out:
        out.append(
            '<div class="banner ok"><b>Clear</b>No stand-down, no expiries '
            "needing action, and every held position is journalled.</div>"
        )
    return "".join(out)


# -------------------------------------------------------------------- board


def board(
    account: AccountSnapshot,
    rules: Rules,
    curve: list[tuple[str, float]],
    open_trades: list[Trade],
    stand_down: StandDownState,
    consecutive_losses: int,
    orders: list[WorkingOrder] | None = None,
    prices: dict[str, float] | None = None,
) -> str:
    equity = account.equity_usd
    open_risk_pct = (account.open_risk_usd / equity * 100) if equity else 0.0
    largest = max(
        (t.planned_risk_usd / equity * 100 for t in open_trades if equity), default=0.0
    )
    unrealised = sum(p.unrealised_pnl_usd for p in account.open_positions)

    tiles = (
        stat("Equity", _money(equity), f"{_money(account.cash_usd)} cash")
        + stat(
            "Unrealised",
            _money(unrealised, sign=True),
            f"across {len(account.open_positions)} position(s)",
            _cls(unrealised),
        )
        + stat(
            "Realised today",
            _money(account.realised_pnl_today_usd, sign=True),
            "closed trades only",
            _cls(account.realised_pnl_today_usd),
        )
        + stat(
            "Open risk",
            _money(account.open_risk_usd),
            "loss if every stop filled at once",
        )
    )

    risk_cards = (
        '<div class="card"><h3>Total open risk</h3>'
        '<p class="note">Every open stop filling at once, as a share of equity.</p>'
        + meter(open_risk_pct, rules.account.max_total_risk_pct, "%")
        + '<p class="note" style="margin-top:.7rem">A proposal that would push '
        "this past the cap is rejected outright, never trimmed to fit.</p></div>"
        '<div class="card"><h3>Largest single trade</h3>'
        '<p class="note">The most any one open position can lose.</p>'
        + meter(largest, rules.account.max_risk_per_trade_pct, "%")
        + '<p class="note" style="margin-top:.7rem">A wider stop buys a smaller '
        "position, never more risk.</p></div>"
        '<div class="card"><h3>Stand-down</h3>'
        + (
            f'<p class="note">Active at stage {stand_down.stage}. Live execution '
            f"is withheld for {stand_down.days_remaining(datetime.now(UTC)):.1f} "
            "more days.</p>"
            if stand_down.is_active(datetime.now(UTC))
            else f'<p class="note">Clear. {consecutive_losses} qualifying loss(es) '
            f"in a row against a trigger of "
            f"{rules.stand_down.consecutive_losses_trigger}.</p>"
        )
        + pips(consecutive_losses, rules.stand_down.consecutive_losses_trigger)
        + '<p class="note" style="margin-top:.7rem">A stand-down suspends money, '
        "not trading. Paper carries on.</p></div>"
        '<div class="card"><h3>Concurrent positions</h3>'
        '<p class="note">Held at once, across every symbol.</p>'
        + meter(
            len(account.open_positions),
            float(rules.account.max_concurrent_positions),
            "",
            0,
        )
        + '<p class="note" style="margin-top:.7rem">Trade frequency is a risk '
        "parameter here, not a performance one.</p></div>"
    )

    return (
        head("Account", "Board", f"as at {_when(datetime.now(UTC))}")
        + f'<div class="grid g4">{tiles}</div>'
        + '<section class="block"><h2>Equity</h2>'
        + _curve(curve)
        + "</section>"
        + '<section class="block"><h2>Risk against the caps</h2>'
        + f'<div class="grid g2">{risk_cards}</div></section>'
        + '<section class="block"><h2>Open positions</h2>'
        + _positions(account, open_trades, equity)
        + "</section>"
        + _working_orders(orders or [], prices or {})
    )


def _working_orders(orders: list[WorkingOrder], prices: dict[str, float]) -> str:
    """Orders resting at the broker, and how far the market is from filling them.

    The bot submits limit orders only, deliberately, so one that does not reach
    its price simply waits. Without this the Board shows no position and no
    explanation, when the truth is that an order is sitting there needing a move
    that may never come.
    """
    if not orders:
        return (
            '<section class="block"><h2>Pending orders</h2>'
            '<div class="scroll"><p class="empty">Nothing resting at the broker. '
            "Every order either filled or was never sent.</p></div></section>"
        )

    rows = ""
    for o in orders:
        price = prices.get(o.symbol)
        gap = o.distance_to_fill(price) if price else None
        gap_text = (
            "n/a"
            if gap is None
            else (f"{gap:+.2f}% away" if abs(gap) > 0.005 else "at the limit")
        )
        # A positive gap means the price still has to travel; that is the
        # difference between waiting and never filling.
        gap_cls = "muted" if gap is None or gap <= 0 else ""

        # Each cell is built before the row, never with a trailing conditional
        # on a multi-part f-string: the ternary binds to the whole expression,
        # not the last fragment, and silently eats the rest of the row.
        limit_cell = (
            f"{o.limit_price:,.4f}" if o.limit_price is not None else "market"
        )
        market_cell = f"{price:,.4f}" if price else "unknown"
        filled_note = (
            f' <span class="muted">({o.filled_qty:g} filled)</span>'
            if o.filled_qty
            else ""
        )
        submitted = _when(o.submitted_at) if o.submitted_at else "unknown"
        status = o.status.value.replace("_", " ")

        rows += (
            f'<tr class="data"><td data-l="Symbol"><b>{_e(o.symbol)}</b></td>'
            f'<td data-l="Side">{_e(o.direction.value)}</td>'
            f'<td data-l="Status"><span class="pill hold">{_e(status)}</span></td>'
            f'<td data-l="Qty" class="r num">{o.qty:g}{filled_note}</td>'
            f'<td data-l="Limit" class="r num">{limit_cell}</td>'
            f'<td data-l="Market" class="r num">{market_cell}</td>'
            f'<td data-l="Needs" class="r num {gap_cls}">{gap_text}</td>'
            f'<td data-l="Submitted">{_e(submitted)}</td></tr>'
        )

    return (
        '<section class="block"><h2>Pending orders</h2>'
        '<div class="scroll"><table><caption>"Needs" is how far the market still '
        "has to move for the limit to fill. A large positive number is an order "
        "that is not going to fill today.</caption>"
        "<thead><tr><th>Symbol</th><th>Side</th><th>Status</th><th class=r>Qty</th>"
        "<th class=r>Limit</th><th class=r>Market</th><th class=r>Needs</th>"
        f"<th>Submitted</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _curve(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return (
            '<div class="curve"><p class="empty">Not enough equity marks to draw a '
            "curve yet. The loop records one per cycle.</p></div>"
        )

    values = [v for _, v in points]
    lo_v, hi_v = min(values), max(values)
    span = (hi_v - lo_v) or 1.0
    lo, hi = lo_v - span * 0.12, hi_v + span * 0.12
    width, height, pad = 1000.0, 240.0, 26.0

    def x(i: int) -> float:
        return 8 + (i / (len(points) - 1)) * (width - 16)

    def y(v: float) -> float:
        return (height - pad) - ((v - lo) / (hi - lo)) * (height - pad - 14)

    line = "".join(
        f"{'M' if i == 0 else 'L'}{x(i):.1f} {y(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    area = f"{line}L{x(len(points) - 1):.1f} {height - pad:.1f}L{x(0):.1f} {height - pad:.1f}Z"
    base = y(values[0])
    label = (
        f"Equity from {_money(values[0])} to {_money(values[-1])} across "
        f"{len(points)} marks."
    )
    return (
        f'<div class="curve"><svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'preserveAspectRatio="none" aria-label="{_e(label)}">'
        f'<path class="area" d="{area}"/>'
        f'<line class="base" x1="8" x2="{width - 8:.0f}" y1="{base:.1f}" y2="{base:.1f}"/>'
        f'<path class="line" d="{line}"/>'
        f'<text class="tick" x="8" y="{y(hi_v) - 5:.1f}">{_money(hi_v)}</text>'
        f'<text class="tick" x="8" y="{y(lo_v) + 13:.1f}">{_money(lo_v)}</text>'
        f'<text class="tick" x="8" y="{height - 8:.0f}">{_e(points[0][0][:10])}</text>'
        f'<text class="tick" x="{width - 8:.0f}" y="{height - 8:.0f}" '
        f'text-anchor="end">{_e(points[-1][0][:10])}</text>'
        "</svg>"
        f'<p class="note" style="margin:.75rem 0 0">The dashed line is where this '
        f"curve started. {len(points)} marks recorded.</p></div>"
    )


def _positions(
    account: AccountSnapshot, open_trades: list[Trade], equity: float
) -> str:
    if not account.open_positions:
        return (
            '<div class="scroll"><p class="empty">Nothing open. Most days the '
            "correct action is none.</p></div>"
        )

    by_symbol = {t.symbol: t for t in open_trades}
    rows: list[str] = []
    for p in account.open_positions:
        trade = by_symbol.get(p.symbol)
        stop = f"{trade.planned_stop:,.4f}" if trade else "unknown"
        risk = _money(trade.planned_risk_usd) if trade else "unknown"
        risk_pct = (
            _pct(trade.planned_risk_usd / equity * 100)
            if trade and equity
            else "unknown"
        )
        current = p.current_price or p.entry_price
        rows.append(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(p.symbol)}</b></td>'
            f'<td data-l="Side">{_e(p.direction.value)}</td>'
            f'<td data-l="Qty" class="r num">{p.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{p.entry_price:,.4f}</td>'
            f'<td data-l="Now" class="r num">{current:,.4f}</td>'
            f'<td data-l="Stop" class="r num">{stop}</td>'
            f'<td data-l="At risk" class="r num">{risk} '
            f'<span class="muted">({risk_pct})</span></td>'
            f'<td data-l="Unrealised" class="r num {_cls(p.unrealised_pnl_usd)}">'
            f"{_money(p.unrealised_pnl_usd, sign=True)}</td></tr>"
            + (
                f'<tr class="why"><td colspan="8"><div class="quote">'
                f"{_e(trade.rationale)}</div></td></tr>"
                if trade and trade.rationale
                else ""
            )
        )

    return (
        '<div class="scroll"><table><caption>A stop reading "unknown" means the '
        "position is held at the broker with no journal entry, so its risk cannot "
        "be counted. That is reported rather than guessed at.</caption>"
        "<thead><tr><th>Symbol</th><th>Side</th><th class=r>Qty</th>"
        "<th class=r>Entry</th><th class=r>Now</th><th class=r>Stop</th>"
        "<th class=r>At risk</th><th class=r>Unrealised</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


# ---------------------------------------------------------------- decisions


def decisions(view: AuditView, *, shown: int = 40) -> str:
    """The agent's decision trail: proposal, gate ruling, outcome.

    This is the only surface on which a REJECTED proposal is visible at all. It
    never becomes a trade, so it reaches neither the journal nor the broker, and
    the reasoning behind a refusal exists nowhere else.
    """
    lede = (
        "Every pass of the loop, newest first: what the model assessed, what it "
        "proposed, what the risk gate ruled and on which grounds, and what "
        "actually reached the broker. A rejected proposal appears here and "
        "nowhere else."
    )
    body = head("Trail", "Decisions", f"{len(view.decisions)} cycles", lede)

    if view.is_degraded:
        body += banners(StandDownState(), [], [], audit=view)

    if not view.decisions:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "No decisions recorded yet. The loop writes one entry per cycle to "
            "<code>audit/</code>, so this fills in once "
            "<code>electrum-bot loop</code> has run.</p></div>"
        )
        return body

    body += '<section class="block">'
    for entry in view.decisions[:shown]:
        body += _cycle(entry)
    body += "</section>"
    return body


STANCE_PILL = {
    "take": "ok",
    "watch": "watch",
    "pass": "hold",
    "blocked": "no",
}


def _considered(entry: DecisionEntry) -> str:
    """Every symbol the model looked at, not only the ones it proposed.

    Without this a quiet cycle records "no proposals" and nothing else, and
    "nothing met the conditions" reads identically to "the loop never looked at
    QQQ". Only one of those is a working bot.
    """
    assessments = entry.decision.assessments
    if not assessments:
        return ""

    rows = ""
    for a in sorted(assessments, key=lambda x: (x.stance != "watch", x.symbol)):
        waiting = ""
        if a.stance == "watch":
            waiting = (
                f'<div class="rung"><span class="lbl">Trigger</span>{_e(a.waiting_for)}</div>'
                if a.waiting_for.strip()
                else '<div class="rung"><span class="lbl">Trigger</span>'
                '<span class="muted">no condition named, so this is a feeling '
                "rather than a plan</span></div>"
            )
        rows += (
            '<div class="considered">'
            f'<div class="what"><b>{_e(a.symbol)}</b>'
            f'<span class="pill {STANCE_PILL.get(a.stance, "hold")}">{_e(a.stance)}</span>'
            "</div>"
            f'<div class="chain"><div class="rung">{_e(a.reasoning)}</div>{waiting}</div>'
            "</div>"
        )

    watching = sum(1 for a in assessments if a.stance == "watch")
    return (
        f'<div class="step"><p class="eyebrow">Considered '
        f"({len(assessments)} symbol(s)"
        + (f", {watching} on watch" if watching else "")
        + f")</p>{rows}</div>"
    )


def _held(entry: DecisionEntry) -> str:
    """Why each open position is still open, and what would end it."""
    plans = entry.decision.position_plans
    if not plans:
        return ""

    rows = ""
    for p in plans:
        pill = {"hold": "ok", "close": "no", "tighten_stop": "watch"}.get(p.action, "hold")
        rows += (
            '<div class="considered">'
            f'<div class="what"><b>{_e(p.symbol)}</b>'
            f'<span class="pill {pill}">{_e(p.action.replace("_", " "))}</span>'
            + (
                ""
                if p.thesis_intact
                else '<span class="pill no">thesis broken</span>'
            )
            + "</div><div class=\"chain\">"
            f'<div class="rung"><span class="lbl">Still in</span>{_e(p.reasoning)}</div>'
            + (
                f'<div class="rung"><span class="lbl">Closes on</span>'
                f"{_e(p.waiting_for)}</div>"
                if p.waiting_for.strip()
                else ""
            )
            + (
                f'<div class="rung"><span class="lbl">Wrong if</span>'
                f"{_e(p.invalidation)}</div>"
                if p.invalidation.strip()
                else ""
            )
            + "</div></div>"
        )

    return (
        '<div class="step"><p class="eyebrow">Open positions reviewed</p>'
        f"{rows}"
        '<p class="note" style="margin-top:.6rem">Advisory only. The loop does '
        "not act on these: closing a position and moving a stop sit outside the "
        "proposal path, so nothing here reached the broker.</p></div>"
    )


def _read(entry: DecisionEntry) -> str:
    """What the model was actually shown when it decided.

    Recorded with the decision rather than reconstructed later. A snapshot taken
    now answers a different question from the one an old cycle raises.
    """
    inputs = entry.decision.inputs
    if inputs is None:
        return ""

    parts = ""
    if inputs.headlines:
        items = "".join(f"<li>{_e(h)}</li>" for h in inputs.headlines[:8])
        more = (
            f'<li class="muted">and {len(inputs.headlines) - 8} more</li>'
            if len(inputs.headlines) > 8
            else ""
        )
        parts += f'<div class="rung"><span class="lbl">Headlines</span><ul class="feed">{items}{more}</ul></div>'
    else:
        parts += (
            '<div class="rung"><span class="lbl">Headlines</span>'
            '<span class="muted">none supplied. Marketaux gates nothing, so this '
            "is context the model did without.</span></div>"
        )

    if inputs.social_posts:
        items = "".join(f"<li>{_e(s)}</li>" for s in inputs.social_posts[:10])
        parts += (
            '<div class="rung"><span class="lbl">Posts</span>'
            f'<ul class="feed">{items}</ul></div>'
        )
    elif inputs.social_degraded:
        parts += (
            '<div class="rung gate no"><span class="lbl">Posts</span>'
            "the social feed was DEGRADED. An empty list here means the fetch "
            "failed, not that nothing was posted.</div>"
        )

    if inputs.news_windows:
        items = "".join(f"<li>{_e(w)}</li>" for w in inputs.news_windows)
        parts += f'<div class="rung"><span class="lbl">Blackouts</span><ul class="feed">{items}</ul></div>'
    elif inputs.calendar_degraded:
        parts += (
            '<div class="rung gate no"><span class="lbl">Blackouts</span>'
            "the earnings calendar was DEGRADED. Zero windows here means the feed "
            "failed, not that there were no announcements, and the blackout rule "
            "could not fire.</div>"
        )
    else:
        parts += (
            '<div class="rung"><span class="lbl">Blackouts</span>'
            '<span class="muted">no announcements inside the window</span></div>'
        )

    if inputs.symbols_without_history:
        parts += (
            '<div class="rung gate no"><span class="lbl">No history</span>'
            f"{_e(', '.join(inputs.symbols_without_history))} had no bars, so no "
            "indicators were computed for them.</div>"
        )

    if inputs.indicators:
        rows = "".join(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(s)}</b></td>'
            f'<td data-l="Reading" class="num">{_e(v)}</td></tr>'
            for s, v in sorted(inputs.indicators.items())
        )
        parts += (
            '<div class="rung"><span class="lbl">Indicators</span>'
            '<div class="scroll" style="margin-top:.4rem"><table><tbody>'
            f"{rows}</tbody></table></div></div>"
        )

    return (
        '<details class="step"><summary class="eyebrow">What it read</summary>'
        f'<div class="chain" style="margin-top:.7rem">{parts}</div></details>'
    )


def _cycle(entry: DecisionEntry) -> str:
    d = entry.decision
    pill = {
        "executed": "ok",
        "approved": "ok",
        "refused": "no",
        "rejected": "no",
        "held": "hold",
    }[entry.outcome]

    cost = ""
    if d.claude_input_tokens or d.claude_output_tokens:
        cost = (
            f"{d.claude_input_tokens:,} in / {d.claude_output_tokens:,} out"
            + (f" / {d.claude_cached_tokens:,} cached" if d.claude_cached_tokens else "")
            + f" &middot; ${d.estimated_cost_usd:.4f}"
        )

    out = (
        '<article class="cycle"><div class="head">'
        f'<span class="when">{_e(_when(entry.timestamp))}</span>'
        f'<span class="pill {pill}">{entry.outcome}</span>'
        f'<span class="note">{len(d.proposals)} proposal(s), '
        f"{entry.approved} approved, {entry.rejected} rejected</span>"
        + (f'<span class="cost">{cost}</span>' if cost else "")
        + "</div>"
    )

    if d.notes:
        out += f'<div class="assessment"><q>{_e(d.notes)}</q></div>'

    out += _considered(entry)
    out += _held(entry)
    out += _read(entry)

    if not d.proposals:
        out += (
            '<div class="step"><p class="note">Nothing proposed. Doing nothing is '
            "a valid output and is recorded as one.</p></div>"
        )

    for i, proposal in enumerate(d.proposals):
        verdict = entry.verdict_for(i)
        out += '<div class="step"><div class="what">'
        out += (
            f"<b>{_e(proposal.direction.value.upper())} {proposal.qty:g} "
            f"{_e(proposal.symbol)}</b>"
            f'<span class="note">limit {proposal.limit_price:,.4f}, stop '
            f"{proposal.stop_loss_price:,.4f}, target "
            f"{proposal.take_profit_price:,.4f}</span>"
            f'<span class="note">risks {_money(proposal.risk_usd)}</span>'
            "</div>"
        )

        out += '<div class="chain">'
        out += (
            '<div class="rung"><span class="lbl">Why</span>'
            f"{_e(proposal.rationale)}</div>"
        )

        if verdict is None:
            out += (
                '<div class="rung"><span class="lbl">Gate</span>'
                '<span class="muted">no verdict recorded against this proposal. '
                "The loop skips a proposal whose symbol has no tick, so the lists "
                "no longer line up and pairing them would be a guess.</span></div>"
            )
        elif verdict.approved:
            out += '<div class="rung gate ok"><span class="lbl">Gate</span>approved</div>'
        else:
            reasons = "".join(f"<li>{_e(r)}</li>" for r in verdict.reasons)
            out += (
                '<div class="rung gate no"><span class="lbl">Gate</span>rejected'
                f'<ul class="reasons">{reasons}</ul></div>'
            )

        result = d.executed[i] if i < len(d.executed) else None
        if result is None:
            out += (
                '<div class="rung"><span class="lbl">Then</span>'
                '<span class="muted">not sent. Either the gate refused it, or the '
                "loop is running without --execute.</span></div>"
            )
        elif result.accepted:
            filled = (
                f"filled {result.filled_qty:g} at {result.filled_price:,.4f}"
                if result.filled_price and result.filled_qty
                else "accepted"
            )
            out += (
                f'<div class="rung"><span class="lbl">Then</span>{filled} '
                f'<span class="muted">({_e(result.order_id or "no id")})</span></div>'
            )
        else:
            out += (
                '<div class="rung gate no"><span class="lbl">Then</span>'
                f"broker refused: {_e(result.error or 'no reason given')}</div>"
            )

        out += "</div></div>"

    return out + "</article>"


# ------------------------------------------------------------------- trades


def trades_page(recent: list[Trade], report: JournalReport) -> str:
    body = head(
        "Journal",
        "Trades",
        f"{report.overall.trade_count} closed",
        "Every closed trade with the reasoning recorded against it at the time. "
        "R is the result as a multiple of what the trade was designed to lose.",
    )

    if not recent:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "No closed trades yet.</p></div>"
        )
        return body

    rows: list[str] = []
    for t in reversed(recent):
        r = t.r_multiple
        exit_price = f"{t.exit_price:,.4f}" if t.exit_price is not None else "n/a"
        exit_date = t.exit_time.date().isoformat() if t.exit_time else "open"
        r_text = f"{r:+.2f}R" if r is not None else "n/a"
        rationale = (
            f'<tr class="why"><td colspan="9"><div class="quote">'
            f"{_e(t.rationale)}</div></td></tr>"
            if t.rationale
            else ""
        )
        rows.append(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(t.symbol)}</b><br>'
            f'<span class="note">{_e(t.strategy)}</span></td>'
            f'<td data-l="Held">{_e(t.entry_time.date().isoformat())}<br>'
            f'<span class="note">to {_e(exit_date)}</span></td>'
            f'<td data-l="Qty" class="r num">{t.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{t.entry_price:,.4f}</td>'
            f'<td data-l="Stop" class="r num">{t.planned_stop:,.4f}</td>'
            f'<td data-l="Exit" class="r num">{exit_price}</td>'
            f'<td data-l="At risk" class="r num">{_money(t.planned_risk_usd)}</td>'
            f'<td data-l="Fees" class="r num muted">{_money(t.fees_usd)}</td>'
            f'<td data-l="Result" class="r num {_cls(t.net_pnl_usd)}">'
            f"{_money(t.net_pnl_usd, sign=True)}<br>"
            f'<span class="note">{r_text}</span></td></tr>' + rationale
        )

    body += (
        '<div class="scroll" style="margin-top:1.5rem"><table>'
        "<thead><tr><th>Symbol</th><th>Held</th><th class=r>Qty</th>"
        "<th class=r>Entry</th><th class=r>Stop</th><th class=r>Exit</th>"
        "<th class=r>At risk</th><th class=r>Fees</th><th class=r>Result</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )
    return body


# ---------------------------------------------------------------- analytics


def analytics_page(report: JournalReport) -> str:
    s = report.overall
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
    er = f"{s.expectancy_r:+.2f}R" if s.expectancy_r is not None else "n/a"

    body = head(
        "Measurement",
        "Analytics",
        f"{s.trade_count} closed trades",
        "A wrong metric is worse than no metric, because it gets believed and "
        "then acted on. Every figure here is computed by src/bot/metrics.py.",
    )
    body += (
        '<div class="grid g4" style="margin-top:1.5rem">'
        + stat("Win rate", f"{s.win_rate:.0%}", f"{s.wins}W / {s.losses}L")
        + stat("Profit factor", pf, _e(s.health))
        + stat(
            "Expectancy",
            _money(s.expectancy_usd, sign=True),
            f"per trade, {er}",
            _cls(s.expectancy_usd),
        )
        + stat(
            "Net",
            _money(s.total_pnl_usd, sign=True),
            f"max drawdown {_money(s.max_drawdown_usd)}",
            _cls(s.total_pnl_usd),
        )
        + "</div>"
    )
    body += (
        '<section class="block"><h2>Headline</h2>'
        f'<div class="readout">{_e(chr(10).join(render_summary(s)))}</div></section>'
        '<section class="block"><h2>Stops and targets, judged after the fact</h2>'
        f'<div class="readout">{_e(chr(10).join(render_excursions(report.excursions)))}'
        "</div></section>"
    )

    for title, table in (
        ("By strategy", report.by_strategy),
        ("By asset class", report.by_asset_class),
        ("By weekday", report.by_weekday),
    ):
        if not table:
            continue
        # The reading column carries `PerformanceSummary.health`, which hedges
        # itself below the thin-sample threshold. Without it a three-trade row
        # showing 67% and +$400 reads as a result, while the headline above —
        # computed from the same code — says the sample is noise. Two figures
        # from one module disagreeing about how much they can be trusted is the
        # sma_200-over-40-bars error in a table.
        rows = "".join(
            f'<tr class="data"><td data-l="Group"><b>{_e(name)}</b></td>'
            f'<td data-l="Trades" class="r num">{g.trade_count}</td>'
            f'<td data-l="Win rate" class="r num">{g.win_rate:.0%}</td>'
            f'<td data-l="Net" class="r num {_cls(g.total_pnl_usd)}">'
            f"{_money(g.total_pnl_usd, sign=True)}</td>"
            f'<td data-l="Reading"{" class=thin" if g.sample_is_thin else ""}>'
            f"{_e(g.health)}</td></tr>"
            for name, g in table.items()
        )
        body += (
            f'<section class="block"><h2>{_e(title)}</h2>'
            '<div class="scroll"><table><thead><tr><th>Group</th>'
            "<th class=r>Trades</th><th class=r>Win rate</th><th class=r>Net</th>"
            "<th>Reading</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div></section>"
        )
    return body


# ----------------------------------------------------------------- settings


def _row(key: str, value: str, why: str = "") -> str:
    return (
        f"<dt>{_e(key)}</dt><dd>{_e(value)}"
        + (f'<span class="why">{_e(why)}</span>' if why else "")
        + "</dd>"
    )


def settings_page(rules: Rules, env: Env, *, chat_enabled: bool) -> str:
    """Structured, and read-only for anything that governs risk.

    A settings screen that could widen a limit would be a settings screen that
    gets used to widen one during a losing run, which is exactly when the limit
    is doing its job. So the limits are shown with their reasoning and with the
    file that owns them, and changing one stays a commit.
    """
    body = head(
        "Configuration",
        "Settings",
        "",
        "What the bot is currently configured to do. The risk limits are shown "
        "read-only on purpose: they change in a commit, by a person, with a "
        "reason, and never from a browser.",
    )

    a = rules.account
    body += (
        '<section class="block"><h2>Risk limits</h2><div class="grid g2">'
        '<div class="card"><h3>Per trade and across the book</h3><dl class="kv">'
        + _row(
            "Per trade",
            f"{a.max_risk_per_trade_pct:.2f}%",
            "Most any single position may lose if its stop fills.",
        )
        + _row(
            "Total open",
            f"{a.max_total_risk_pct:.2f}%",
            "Most everything open may lose at once. Measured in risk, not "
            "notional, which is what makes it leverage-neutral.",
        )
        + _row(
            "Concentration",
            f"{a.max_position_pct:.1f}%",
            "A backstop on one position's market value. Deliberately generous; "
            "it is not meant to be the binding constraint.",
        )
        + _row("Concurrent positions", str(a.max_concurrent_positions))
        + _row(
            "Daily loss kill",
            f"{a.daily_loss_kill_pct:.2f}%",
            "Sticky for the session. A recovery within the same day does not "
            "re-enable trading.",
        )
        + _row("Equity floor", _money(a.min_equity_floor_usd), "Below this the bot halts.")
        + '</dl><p class="source">Owned by <code>config/rules.yaml</code>, '
        "enforced in <code>src/bot/risk.py</code>.</p></div>"
        '<div class="card"><h3>Anti-churn</h3><dl class="kv">'
        + _row("Trades per day", str(rules.frequency.max_trades_per_day))
        + _row("Trades per week", str(rules.frequency.max_trades_per_week))
        + _row(
            "Per-symbol cooldown",
            f"{rules.frequency.min_seconds_between_trades_per_symbol}s",
            "Prevents flip-flopping on one name.",
        )
        + _row(
            "News blackout",
            f"{rules.news_blackout_minutes_before}m before / "
            f"{rules.news_blackout_minutes_after}m after",
        )
        + '</dl><p class="source">Fees, not stock picking, were the dominant loss '
        "driver in the Alpha Arena competition. These are risk controls.</p></div>"
        '<div class="card"><h3>Stand-down</h3><dl class="kv">'
        + _row("Trigger", f"{rules.stand_down.consecutive_losses_trigger} losses in a row")
        + _row(
            "Scratch threshold",
            f"{rules.stand_down.loss_threshold_r:.2f}R",
            "A smaller loss neither counts toward the streak nor resets it.",
        )
        + _row("First trip", f"{rules.stand_down.stage_one_days} days")
        + _row(
            "Repeat trip",
            f"{rules.stand_down.stage_two_days} days",
            f"if inside {rules.stand_down.repeat_window_days} days of the last one",
        )
        + '</dl><p class="source">Live execution is withheld. Paper trading '
        "continues, and closing a position is never gated.</p></div>"
        '<div class="card"><h3>Margin</h3><dl class="kv">'
        + _row(
            "Buying-power use",
            f"{rules.margin.max_buying_power_utilisation_pct:.1f}%",
            "Share of available buying power one order may consume.",
        )
        + _row(
            "Gross exposure",
            f"{rules.margin.max_gross_notional_pct:.1f}%",
            "Reg T permits 200% overnight; this leaves headroom.",
        )
        + '</dl><p class="source">These replace the Pattern Day Trader rule, which '
        "FINRA retired on 2026-06-04. What applies now is Intraday Margin "
        "Deficit calls.</p></div></div></section>"
    )

    instrument_cards = ""
    for name, inst in rules.instruments.items():
        sessions = inst.render_sessions()
        # Shown beside the hours rather than folded into them. The hours alone
        # read as "this is when it trades", and for three quarters of a year
        # that is wrong by two days a week.
        days = ", ".join(DAY_NAMES[d] for d in sorted(inst.session_days_utc) if 0 <= d <= 6)
        instrument_cards += (
            f'<div class="card"><h3>{_e(name)}</h3><dl class="kv">'
            + _row("Enabled", "yes" if inst.enabled else "no")
            + _row("Strategy", inst.strategy)
            + _row("Symbols", ", ".join(inst.allowed_symbols) or "none")
            + _row("Sessions (UTC)", sessions or "none")
            + _row("Trading days", days or "none")
            + (
                _row("Capital cap", f"{inst.capital_cap_pct:.1f}%")
                if inst.capital_cap_pct is not None
                else ""
            )
            + "</dl></div>"
        )
    open_now = rules.classes_in_session(datetime.now(UTC))
    skip = rules.loop.skip_model_call_when_all_markets_closed
    body += (
        '<section class="block"><h2>Loop</h2>'
        '<div class="card"><dl class="kv">'
        + _row("Open right now", ", ".join(open_now) or "nothing")
        + _row(
            "Skip the model call when everything is shut",
            "yes" if skip else "no",
        )
        + "</dl>"
        '<p class="source">Not a risk limit. The worst a wrong value here does is '
        "stop the model being asked; it cannot widen what the gate allows. The "
        "open window is derived from the instrument classes below, so enabling a "
        "class that trades at the weekend widens it with nothing else to change. "
        "Owned by config/rules.yaml.</p></div></section>"
    )

    body += (
        '<section class="block"><h2>Instruments</h2>'
        '<p class="note">Each class carries its own session window, symbol list '
        "and strategy. The portfolio limits above stay global.</p>"
        f'<div class="grid g2" style="margin-top:1rem">{instrument_cards}</div></section>'
    )

    # Credentials are reported as present or absent, never shown. This page is
    # loopback-bound, but a screenshot is not.
    body += (
        '<section class="block"><h2>Runtime</h2><div class="grid g2">'
        '<div class="card"><h3>Execution</h3><dl class="kv">'
        + _row(
            "Mode",
            "paper" if env.alpaca_paper_trade else "LIVE",
            "The code refuses to start twice over if this is ever false.",
        )
        + _row("Decision interval", f"{env.decision_interval_seconds}s")
        + _row("Model tier", env.claude_tier.value)
        + '</dl><p class="source">Whether orders are actually placed is decided by '
        "the <code>--execute</code> flag on the service unit, not from here.</p></div>"
        '<div class="card"><h3>Credentials and feeds</h3><dl class="kv">'
        + _row("Alpaca", "configured" if env.alpaca_api_key else "not configured")
        + _row("Anthropic", "configured" if env.anthropic_api_key else "not configured")
        + _row(
            "Finnhub",
            "configured" if env.finnhub_api_key else "not configured",
            "Feeds the earnings blackout. Without it that rule cannot fire.",
        )
        + _row(
            "Marketaux",
            "configured" if env.marketaux_api_key else "not configured",
            "Headlines only. Gates nothing.",
        )
        + _row(
            "X posts",
            (
                "off in rules.yaml"
                if not rules.social.enabled
                else ("configured" if env.x_bearer_token else "enabled but no token")
            ),
            (
                f"Watching {', '.join(rules.social.accounts)}. Context only."
                if rules.social.enabled and rules.social.accounts
                else "Accounts to watch live in the social block of rules.yaml."
            ),
        )
        + _row("Dashboard chat", "on" if chat_enabled else "off")
        + '</dl><p class="source">Presence only. No key is rendered on this page, '
        "on any surface, at any time.</p></div></div></section>"
    )
    return body


# --------------------------------------------------------------------- chat


def chat_page(*, enabled: bool, token: str, hermes_available: bool) -> str:
    body = head(
        "Command centre",
        "Chat",
        "",
        "Hermes reaches the bot through the MCP tools, so every order-placing "
        "tool behind this re-runs the risk gate first. It answers questions and "
        "cannot route around anything.",
    )

    if not enabled:
        return body + (
            '<div class="banner warn" style="margin-top:1.5rem"><b>Chat is off</b>'
            "Set <code>DASHBOARD_CHAT_TOKEN</code> in the environment to enable it. "
            "This is off by default on purpose: the rest of this dashboard only "
            "<em>displays</em> an account, while chat can <em>drive an agent</em> "
            "that reaches the broker. Exposure here risks action rather than "
            "disclosure, so switching it on should be a decision and never a side "
            "effect of deploying.</div>"
        )

    if not hermes_available:
        return body + (
            '<div class="banner crit" style="margin-top:1.5rem"><b>Hermes not found</b>'
            "The token is set, but the Hermes binary is not installed where this "
            "process expects it. See <code>docs/HERMES_SETUP.md</code>.</div>"
        )

    suggestions = [
        "What is my open risk right now, and how close is it to the cap?",
        "Why was the last proposal rejected?",
        "What news has the bot seen today, and how old is it?",
        "Which rejection reason has fired most often, and on what?",
        "How many times have we watched a symbol without naming a trigger?",
        "Summarise this week's trades and what closed them.",
        "Is anything expiring soon that needs action?",
    ]
    chips = "".join(
        f'<button type="button" data-q="{_e(q)}">{_e(q)}</button>' for q in suggestions
    )

    return body + f"""
<div class="chat" style="margin-top:1.5rem">
  <div class="log" id="log" aria-live="polite" aria-label="Conversation">
    <!-- Deliberately one line. What this message used to say is already said
         twice on the page: the note above states what Hermes reaches and that
         it cannot route around the gate, and the buttons below are a better
         list of what to ask than a sentence describing one. -->
    <div class="turn agent"><span class="who">Hermes</span>
      <div class="msg">Active. Ask away.</div></div>
  </div>
  <div class="prompts">{chips}</div>
  <div class="composer">
    <textarea id="msg" rows="2" placeholder="Ask about the account, a trade, a rejection, or the news"
      aria-label="Message"></textarea>
    <button class="btn" id="send" type="submit">Send</button>
  </div>
  <p class="note">Turns are replayed for continuity but this is not a long-lived
  session. Hermes keeps its own memory; the dashboard does not.</p>
  <p class="note"><b>News here is a recording, not a search.</b> Hermes has no
  web access, deliberately. What it can read is what the trading loop was shown
  and wrote down each cycle &mdash; headlines, posts from watched accounts and
  earnings windows &mdash; with the age of each attached. Asking does not fetch
  anything: the Marketaux free tier is 100 requests a day against a loop that
  wakes 96 times, so that quota belongs to the loop.</p>
  <p class="note"><b>The whole history is searchable, not just recent days.</b>
  Every cycle, assessment, rejection reason and headline is indexed into
  <code>data/insight.db</code>, and Hermes queries it with read-only SQL. So
  &ldquo;what did we decide about AAPL in March&rdquo; and &ldquo;which rule
  refuses proposals most often&rdquo; are answerable. The index is derived from
  <code>audit/</code> and rebuilt from it, so the log stays the record and the
  index is only ever a faster way to read it.</p>
</div>
<script>
(function () {{
  var TOKEN = {json.dumps(token)};
  var log = document.getElementById('log');
  var box = document.getElementById('msg');
  var send = document.getElementById('send');
  var history = [];

  function turn(who, text, cls) {{
    var el = document.createElement('div');
    el.className = 'turn ' + (cls || '');
    var w = document.createElement('span'); w.className = 'who'; w.textContent = who;
    var m = document.createElement('div'); m.className = 'msg'; m.textContent = text;
    el.appendChild(w); el.appendChild(m); log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return m;
  }}

  function ask() {{
    var text = box.value.trim();
    if (!text) return;
    box.value = '';
    turn('You', text, 'user');
    var pending = turn('Hermes', 'Thinking. A question that fans out across the '
      + 'MCP tools can take a while.', 'agent');
    send.disabled = true;

    fetch('/chat', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ token: TOKEN, message: text, history: history }})
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d.ok) {{
          pending.textContent = d.text;
          history.push({{ user: text, agent: d.text }});
        }} else {{
          pending.parentElement.className = 'turn err';
          pending.textContent = d.error || 'Hermes returned nothing.';
        }}
      }})
      .catch(function (e) {{
        pending.parentElement.className = 'turn err';
        pending.textContent = String(e);
      }})
      .finally(function () {{ send.disabled = false; box.focus(); }});
  }}

  send.addEventListener('click', ask);
  box.addEventListener('keydown', function (e) {{
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {{ e.preventDefault(); ask(); }}
  }});
  document.querySelectorAll('.prompts button').forEach(function (b) {{
    b.addEventListener('click', function () {{ box.value = b.dataset.q; ask(); }});
  }});
}})();
</script>"""


# -------------------------------------------------------------------- login


def login_page(*, env: Env, error: str = "") -> str:
    """The gate. Deliberately says nothing about the account behind it.

    No equity, no positions, no symbol list, not even whether a trade has ever
    been placed. Everything this page renders is already public in the GitHub
    repository, so an unauthenticated visitor learns nothing they could not
    read there. That is the whole job of a sign-in screen and it is easy to
    lose by putting a friendly summary above the form.
    """
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    message = (
        f'<p class="note" style="color:var(--loss)">{_e(error)}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>Sign in &middot; Mudhorn Capital</title><style>{STYLES}</style></head><body>
<header class="bar"><div class="wrap">
  <span class="brand">{MARK} MUDHORN <span class="thin">CAPITAL</span></span>
  <span class="live paper"><i></i>{_e(mode)}</span>
</div></header>
<main><div class="wrap" style="max-width:26rem;margin-top:4rem">
  <h1 style="font-size:1.5rem">Operator sign-in</h1>
  <p class="note">Live command centre for a private paper-trading account.
  Not a demo.</p>
  {message}
  <form method="post" action="/login" style="margin-top:1.5rem">
    <label for="password" class="eyebrow">Password</label>
    <input id="password" name="password" type="password"
      autocomplete="current-password" required autofocus
      style="width:100%;margin-top:.5rem">
    <button type="submit" style="margin-top:1rem;width:100%">Sign in</button>
  </form>
</div></main>
<footer class="wrap">Paper trading only. Rendered
{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC.</footer>
</body></html>"""
