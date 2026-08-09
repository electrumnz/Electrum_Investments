/* ==========================================================================
   Mudhorn Capital web app: shared formatting, metrics and charts.

   Reads window.MUDHORN_DEMO, which is a committed fixture. There is no fetch
   in this file and there is no API to fetch from. See brand/README.md.

   The demo banner and the masthead are plain HTML in every page rather than
   rendered here on purpose: the label saying these figures are invented must
   not depend on a script having run.

   The metric definitions below mirror src/bot/metrics.py deliberately, down to
   returning null for an undefined profit factor rather than Infinity, and down
   to excluding unsampled trades from the excursion analysis. If the two ever
   disagree, that file is right and this one is wrong.
   ========================================================================== */

(function () {
  'use strict';

  var DATA = window.MUDHORN_DEMO;

  /* Below this many closed trades, expectancy and profit factor are noise.
     Same threshold as metrics.THIN_SAMPLE_THRESHOLD. */
  var THIN_SAMPLE = 20;

  /* ------------------------------------------------------------ formatting */

  /* Every formatter returns "n/a" rather than a number for a value that is
     null, undefined or NaN.

     This is the same rule as src/bot/metrics.py, and it is not defensive
     padding. Several of these figures are legitimately undefined: a profit
     factor with no losing trades, an R-multiple on a trade with no planned
     risk, a capture ratio with no winners to measure. Formatting those
     unguarded prints "$NaN", which reads as a rendering glitch rather than as
     missing data, and "NaN" sorts and scans like a number. The repository's
     rule is to report missing data rather than invent a plausible value, and
     an unguarded formatter breaks that rule at the last possible moment,
     after every layer above it got the answer right. */
  function absent(value) {
    return value === null || value === undefined || (typeof value === 'number' && isNaN(value));
  }

  function usd(value, opts) {
    if (absent(value)) return 'n/a';
    var o = opts || {};
    var digits = o.digits === undefined ? 2 : o.digits;
    var abs = Math.abs(value).toLocaleString('en-GB', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
    var sign = value < 0 ? '-' : (o.signed ? '+' : '');
    return sign + '$' + abs;
  }

  function pct(value, digits) {
    if (absent(value)) return 'n/a';
    var d = digits === undefined ? 2 : digits;
    return value.toFixed(d) + '%';
  }

  function signedPct(value, digits) {
    if (absent(value)) return 'n/a';
    return (value > 0 ? '+' : '') + pct(value, digits);
  }

  function rMultiple(value) {
    if (absent(value)) return 'n/a';
    return (value > 0 ? '+' : '') + value.toFixed(2) + 'R';
  }

  function ratio(value, digits) {
    if (absent(value)) return 'n/a';
    return value.toFixed(digits === undefined ? 2 : digits);
  }

  function qty(value) {
    if (absent(value)) return 'n/a';
    return Number(value).toLocaleString('en-GB', { maximumFractionDigits: 4 });
  }

  /* British English throughout, and dates spelled out rather than numeric, so
     08/09 is never ambiguous between two readings. */
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function shortDate(iso) {
    var d = new Date(iso);
    return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
  }

  function shortDateTime(iso) {
    var d = new Date(iso);
    var hh = String(d.getUTCHours()).padStart(2, '0');
    var mm = String(d.getUTCMinutes()).padStart(2, '0');
    return shortDate(iso) + ', ' + hh + ':' + mm + ' UTC';
  }

  function monthLabel(iso) {
    var d = new Date(iso);
    return MONTHS[d.getUTCMonth()] + ' ' + String(d.getUTCFullYear()).slice(2);
  }

  function signClass(value) {
    if (value > 0) return 'gain';
    if (value < 0) return 'loss';
    return 'muted';
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* --------------------------------------------------------------- metrics */

  function mean(values) {
    if (!values.length) return 0;
    var total = 0;
    for (var i = 0; i < values.length; i++) total += values[i];
    return total / values.length;
  }

  /* Largest peak-to-trough fall in cumulative P&L, returned positive. Walks
     the trades in the order they closed, which is what makes it a drawdown
     rather than a worst trade. */
  function maxDrawdown(pnls) {
    var peak = 0, equity = 0, worst = 0;
    for (var i = 0; i < pnls.length; i++) {
      equity += pnls[i];
      if (equity > peak) peak = equity;
      if (peak - equity > worst) worst = peak - equity;
    }
    return worst;
  }

  function summarise(trades) {
    var closed = trades.slice().sort(function (a, b) {
      return a.exitTime < b.exitTime ? -1 : 1;
    });

    /* Carries every key the populated branch does, including rValues and
       feesUsd. An empty-case object missing a key is how a caller ends up
       reading `undefined.length` on a page that renders fine against the
       shipped fixture and breaks the moment a filter matches nothing. */
    if (!closed.length) {
      return { tradeCount: 0, wins: 0, losses: 0, winRate: 0, profitFactor: null,
               expectancyUsd: 0, expectancyR: null, totalPnlUsd: 0, avgR: null,
               avgWinUsd: 0, avgLossUsd: 0, grossProfitUsd: 0, grossLossUsd: 0,
               bestUsd: null, worstUsd: null, maxDrawdownUsd: 0, payoffRatio: null,
               rValues: [], feesUsd: 0,
               thin: true, health: 'no closed trades yet' };
    }

    var pnls = closed.map(function (t) { return t.netPnlUsd; });
    var wins = pnls.filter(function (p) { return p > 0; });
    var losses = pnls.filter(function (p) { return p < 0; });

    var grossProfit = wins.reduce(function (a, b) { return a + b; }, 0);
    var grossLoss = Math.abs(losses.reduce(function (a, b) { return a + b; }, 0));

    var rValues = closed
      .filter(function (t) { return typeof t.rMultiple === 'number'; })
      .map(function (t) { return t.rMultiple; });

    var winRate = wins.length / closed.length;
    var avgWin = mean(wins);
    var avgLoss = Math.abs(mean(losses));

    /* Guarded exactly as metrics.py guards it: no losing trades means the
       ratio is undefined, not infinite. Both Infinity and 0.0 read as a real
       number to somebody skimming, and one of them reads as terrible. */
    var profitFactor = grossLoss > 0 ? grossProfit / grossLoss : null;

    var summary = {
      tradeCount: closed.length,
      wins: wins.length,
      losses: losses.length,
      winRate: winRate,
      avgWinUsd: avgWin,
      avgLossUsd: avgLoss,
      grossProfitUsd: grossProfit,
      grossLossUsd: grossLoss,
      profitFactor: profitFactor,
      payoffRatio: avgLoss > 0 ? avgWin / avgLoss : null,
      expectancyUsd: (winRate * avgWin) - ((1 - winRate) * avgLoss),
      expectancyR: rValues.length ? mean(rValues) : null,
      totalPnlUsd: pnls.reduce(function (a, b) { return a + b; }, 0),
      avgR: rValues.length ? mean(rValues) : null,
      rValues: rValues,
      bestUsd: Math.max.apply(null, pnls),
      worstUsd: Math.min.apply(null, pnls),
      maxDrawdownUsd: maxDrawdown(pnls),
      feesUsd: closed.reduce(function (a, t) { return a + t.feesUsd; }, 0),
      thin: closed.length < THIN_SAMPLE
    };
    summary.health = healthOf(summary);
    return summary;
  }

  /* Plain reading of profit factor, hedged when the sample is thin. Same
     wording as PerformanceSummary.health. */
  function healthOf(s) {
    if (s.tradeCount === 0) return 'no closed trades yet';
    if (s.profitFactor === null) return 'no losing trades yet across ' + s.tradeCount;
    var verdict;
    if (s.profitFactor >= 2.0) verdict = 'strong';
    else if (s.profitFactor >= 1.5) verdict = 'solid';
    else if (s.profitFactor > 1.0) verdict = 'marginally profitable';
    else verdict = 'losing money';
    if (s.thin) return verdict + ', but only ' + s.tradeCount + ' trades so treat as noise';
    return verdict;
  }

  /* Only trades that actually recorded an excursion are included. A trade
     opened and closed inside one decision interval has never been sampled, and
     counting its zeros would drag both ratios toward zero and invent a
     verdict out of nothing. */
  function analyseExcursions(trades) {
    var sampled = trades.filter(function (t) { return t.maeUsd !== 0 || t.mfeUsd !== 0; });
    /* Same completeness rule as summarise: the averages are null rather than
       absent, so a caller formatting them gets "n/a" instead of "$NaN". */
    if (!sampled.length) {
      return { sampledTrades: 0, avgMfeUsd: null, avgRealisedWinUsd: null,
               captureRatio: null, avgMaeUsd: null, avgPlannedRiskUsd: null,
               maeToRiskRatio: null };
    }

    var winners = sampled.filter(function (t) { return t.netPnlUsd > 0; });
    var avgMfe = mean(winners.map(function (t) { return t.mfeUsd; }));
    var avgRealisedWin = mean(winners.map(function (t) { return t.netPnlUsd; }));
    var avgMae = mean(sampled.map(function (t) { return t.maeUsd; }));
    var risks = sampled.map(function (t) { return t.plannedRiskUsd; })
                       .filter(function (r) { return r > 0; });
    var avgRisk = mean(risks);

    return {
      sampledTrades: sampled.length,
      avgMfeUsd: avgMfe,
      avgRealisedWinUsd: avgRealisedWin,
      captureRatio: avgMfe > 0 ? avgRealisedWin / avgMfe : null,
      avgMaeUsd: avgMae,
      avgPlannedRiskUsd: avgRisk,
      maeToRiskRatio: avgRisk > 0 ? Math.abs(avgMae) / avgRisk : null
    };
  }

  function targetVerdict(a) {
    if (a.captureRatio === null) return 'not enough winning trades to judge target placement';
    var shown = Math.round(a.captureRatio * 100) + '%';
    if (a.captureRatio < 0.5) {
      return 'targets look too close: capturing ' + shown + ' of the favourable move on winners';
    }
    if (a.captureRatio < 0.75) return 'some room to run further: capturing ' + shown;
    return 'targets look well placed: capturing ' + shown;
  }

  function stopVerdict(a) {
    if (a.maeToRiskRatio === null) return 'not enough trades to judge stop placement';
    var shown = Math.round(a.maeToRiskRatio * 100) + '%';
    if (a.maeToRiskRatio >= 0.9) {
      return 'stops look too tight: trades routinely draw down to ' + shown +
             ' of the planned risk, so noise is hitting them';
    }
    if (a.maeToRiskRatio >= 0.6) return 'stops are being tested: average drawdown reaches ' + shown;
    return 'stops have comfortable room: average drawdown ' + shown;
  }

  /* ---------------------------------------------------------------- charts */
  /* Inline SVG built from the data, no library. Each chart carries role="img"
     and a spoken summary, because a line nobody can see is not a figure. */

  function scale(value, lo, hi, outLo, outHi) {
    if (hi === lo) return (outLo + outHi) / 2;
    return outLo + ((value - lo) / (hi - lo)) * (outHi - outLo);
  }

  function equityChart(curve, opts) {
    var o = opts || {};
    var W = 1000, H = o.height || 280, padL = 8, padR = 8, padT = 16, padB = 26;
    var values = curve.map(function (p) { return p.equity; });
    var loValue = Math.min.apply(null, values);
    var hiValue = Math.max.apply(null, values);
    var span = hiValue - loValue || 1;
    var lo = loValue - span * 0.12;
    var hi = hiValue + span * 0.12;

    var x = function (i) { return scale(i, 0, curve.length - 1, padL, W - padR); };
    var y = function (v) { return scale(v, lo, hi, H - padB, padT); };

    var line = '';
    for (var i = 0; i < curve.length; i++) {
      line += (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(curve[i].equity).toFixed(1);
    }
    var area = line + 'L' + x(curve.length - 1).toFixed(1) + ' ' + (H - padB) +
               'L' + x(0).toFixed(1) + ' ' + (H - padB) + 'Z';

    var startEquity = o.baseline === undefined ? curve[0].equity : o.baseline;
    var baseY = y(startEquity).toFixed(1);

    var last = curve[curve.length - 1].equity;
    var change = last - startEquity;
    var label = 'Equity from ' + usd(startEquity, { digits: 0 }) + ' on ' +
                shortDate(curve[0].date) + ' to ' + usd(last, { digits: 0 }) + ' on ' +
                shortDate(curve[curve.length - 1].date) + ', a change of ' +
                usd(change, { signed: true, digits: 0 }) + '.';

    var ticks = '';
    var everyN = Math.max(1, Math.floor(curve.length / 6));
    for (var t = 0; t < curve.length; t += everyN) {
      ticks += '<text class="tick" x="' + x(t).toFixed(1) + '" y="' + (H - 8) +
               '" text-anchor="middle">' + monthLabel(curve[t].date) + '</text>';
    }

    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
           'preserveAspectRatio="none" aria-label="' + escapeHtml(label) + '">' +
           '<path class="area" d="' + area + '"/>' +
           '<line class="baseline" x1="' + padL + '" x2="' + (W - padR) +
           '" y1="' + baseY + '" y2="' + baseY + '"/>' +
           '<path class="line" d="' + line + '"/>' +
           ticks +
           /* Labelled at the real high and low of the series rather than at the
              padded ends of the axis, so the two numbers on the chart are two
              numbers the account actually reached. */
           '<text class="tick" x="' + padL + '" y="' + (y(hiValue) - 6).toFixed(1) + '">' +
           usd(hiValue, { digits: 0 }) + '</text>' +
           '<text class="tick" x="' + padL + '" y="' + (y(loValue) + 14).toFixed(1) + '">' +
           usd(loValue, { digits: 0 }) + '</text>' +
           '</svg>';
  }

  /* Underwater curve: how far below the running high-water mark the account
     sat on each day. Always at or below zero by construction. */
  function drawdownChart(curve) {
    var W = 1000, H = 180, padL = 8, padR = 8, padT = 12, padB = 26;
    var peak = -Infinity;
    var under = curve.map(function (p) {
      if (p.equity > peak) peak = p.equity;
      return ((p.equity - peak) / peak) * 100;
    });
    var worst = Math.min.apply(null, under);
    var lo = Math.min(worst * 1.2, -0.5);

    var x = function (i) { return scale(i, 0, under.length - 1, padL, W - padR); };
    var y = function (v) { return scale(v, lo, 0, H - padB, padT); };

    var line = '';
    for (var i = 0; i < under.length; i++) {
      line += (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(under[i]).toFixed(1);
    }
    var area = line + 'L' + x(under.length - 1).toFixed(1) + ' ' + y(0).toFixed(1) +
               'L' + x(0).toFixed(1) + ' ' + y(0).toFixed(1) + 'Z';

    var label = 'Drawdown from the running high-water mark. The deepest point was ' +
                worst.toFixed(2) + ' per cent.';

    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
           'preserveAspectRatio="none" aria-label="' + escapeHtml(label) + '">' +
           '<path class="under" d="' + area + '"/>' +
           '<path class="under-line" d="' + line + '"/>' +
           '<line class="axis" x1="' + padL + '" x2="' + (W - padR) +
           '" y1="' + y(0).toFixed(1) + '" y2="' + y(0).toFixed(1) + '"/>' +
           '<text class="tick" x="' + padL + '" y="' + (H - padB - 4) + '">' +
           worst.toFixed(1) + '%</text>' +
           '</svg>';
  }

  /* R-multiple distribution. The bucket at exactly -1R is where a trade that
     stopped out as planned lands, so it should be the tallest bar on the left
     of any system whose stops are being respected. */
  function rHistogram(rValues) {
    var W = 1000, H = 220, padL = 8, padR = 8, padT = 16, padB = 34;
    var step = 0.25, lo = -1.5, hi = 2.0;
    var buckets = [];
    for (var edge = lo; edge < hi - 1e-9; edge += step) {
      buckets.push({ from: edge, to: edge + step, count: 0 });
    }
    rValues.forEach(function (r) {
      var clamped = Math.min(Math.max(r, lo), hi - 1e-9);
      var index = Math.min(buckets.length - 1, Math.floor((clamped - lo) / step));
      buckets[index].count += 1;
    });

    var tallest = Math.max.apply(null, buckets.map(function (b) { return b.count; })) || 1;
    var barW = (W - padL - padR) / buckets.length;
    var bars = '', axis = '';

    buckets.forEach(function (b, i) {
      var h = (b.count / tallest) * (H - padT - padB);
      var bx = padL + i * barW;
      var by = H - padB - h;
      bars += '<rect class="bar' + (b.to <= 0 ? ' neg' : '') + '" x="' + (bx + 1.5).toFixed(1) +
              '" y="' + by.toFixed(1) + '" width="' + (barW - 3).toFixed(1) +
              '" height="' + Math.max(h, b.count ? 2 : 0).toFixed(1) + '"/>';
      if (Math.abs(b.from % 0.5) < 1e-9) {
        axis += '<text class="tick" x="' + bx.toFixed(1) + '" y="' + (H - 12) +
                '" text-anchor="middle">' + b.from.toFixed(1) + 'R</text>';
      }
    });

    var atMinusOne = rValues.filter(function (r) { return r <= -0.875 && r >= -1.125; }).length;
    var label = 'Distribution of ' + rValues.length + ' trade results in R. ' +
                atMinusOne + ' landed at about minus one R, which is a stop filling as planned.';

    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
           'aria-label="' + escapeHtml(label) + '">' + bars +
           '<line class="axis" x1="' + padL + '" x2="' + (W - padR) +
           '" y1="' + (H - padB) + '" y2="' + (H - padB) + '"/>' + axis + '</svg>';
  }

  function sparkline(values) {
    var W = 90, H = 22;
    var lo = Math.min.apply(null, values);
    var hi = Math.max.apply(null, values);
    var path = '';
    for (var i = 0; i < values.length; i++) {
      var x = scale(i, 0, values.length - 1, 1, W - 1);
      var y = scale(values[i], lo, hi, H - 2, 2);
      path += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }
    var dir = values[values.length - 1] >= values[0] ? 'up' : 'down';
    return '<svg class="spark" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true">' +
           '<path class="' + dir + '" d="' + path + '"/></svg>';
  }

  /* ------------------------------------------------------------- fragments */

  function stat(label, value, meta, cls) {
    return '<div class="card stat">' +
             '<span class="label">' + escapeHtml(label) + '</span>' +
             '<span class="value ' + (cls || '') + '">' + value + '</span>' +
             (meta ? '<span class="meta">' + meta + '</span>' : '') +
           '</div>';
  }

  function meter(used, cap, unit, digits) {
    var d = digits === undefined ? 2 : digits;
    var ratio = Math.min(used / cap, 1);
    var over = used > cap;
    return '<div class="meter">' +
             '<div class="track"><div class="fill' + (over ? ' over' : '') +
             '" style="width:' + (ratio * 100).toFixed(1) + '%"></div></div>' +
             '<div class="legend"><span>' + used.toFixed(d) + unit + ' used</span>' +
             '<span>' + cap.toFixed(d) + unit + ' cap</span></div>' +
           '</div>';
  }

  function pips(lit, total) {
    var out = '<div class="pips" role="img" aria-label="' + lit + ' of ' + total +
              ' qualifying losses toward the stand-down trigger">';
    for (var i = 0; i < total; i++) out += '<i class="' + (i < lit ? 'lit' : '') + '"></i>';
    return out + '</div>';
  }

  /* --------------------------------------------------------------- chrome */

  /* The sign-in form sets a display name and nothing else. It is not a session
     and it does not gate anything: every page here is reachable without it,
     which is stated on the sign-in page itself. */
  function currentOperator() {
    try {
      return window.sessionStorage.getItem('mudhorn.demo.operator') || DATA.operator.name;
    } catch (e) {
      return DATA.operator.name;
    }
  }

  function mountWhoami() {
    var node = document.querySelector('[data-whoami]');
    if (node) node.textContent = currentOperator();
  }

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  window.MUDHORN = {
    data: DATA,
    THIN_SAMPLE: THIN_SAMPLE,
    usd: usd, pct: pct, signedPct: signedPct, rMultiple: rMultiple, qty: qty,
    shortDate: shortDate, shortDateTime: shortDateTime, monthLabel: monthLabel,
    signClass: signClass, escapeHtml: escapeHtml, mean: mean,
    summarise: summarise, analyseExcursions: analyseExcursions,
    targetVerdict: targetVerdict, stopVerdict: stopVerdict,
    equityChart: equityChart, drawdownChart: drawdownChart,
    rHistogram: rHistogram, sparkline: sparkline,
    ratio: ratio, absent: absent,
    stat: stat, meter: meter, pips: pips,
    currentOperator: currentOperator, ready: ready
  };

  ready(mountWhoami);
})();
