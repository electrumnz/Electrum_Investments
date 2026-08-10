/* ==========================================================================
   Mudhorn Capital: the projection layer.

   Starfield, hyperspace jump between pages, panel materialisation and the boot
   readout. The same engine as src/bot/web/render.py SCRIPT, so the public site
   and the live command centre move the same way; the two are deliberately
   separate files because one is served statically and the other is inlined by
   a Python string, which is the same split app.css and render.STYLES already
   live with.

   ## The one rule

   It is decoration, and it is written so it cannot stop being decoration.
   Everything it does is additive: it creates its own layers, adds its own
   classes, and if it throws on line one the site behind it renders every page
   exactly as before. Nothing here is asked to REVEAL content, because a reveal
   that fails leaves a blank page.

   Two things it must never touch, both for the same reason: the demo banner
   and the figures. The banner is plain HTML in all six files precisely so the
   label saying these numbers are invented cannot depend on a script having
   run, and no animation here is allowed to delay, hide or reformat a figure.

   ## Reduced motion

   Checked first and answered by doing nothing at all. No canvas, no observer,
   no overlay, no class.
   ========================================================================== */

(function () {
  'use strict';

  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  if (reduced && reduced.matches) return;

  var doc = document.documentElement;
  var body = document.body;
  var store = {
    get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} },
    drop: function (k) { try { sessionStorage.removeItem(k); } catch (e) {} }
  };

  /* ------------------------------------------------------------- layers */

  function layer(cls, id) {
    var el = document.createElement('div');
    el.className = cls ? 'fx ' + cls : 'fx';
    if (id) el.id = id;
    el.setAttribute('aria-hidden', 'true');
    body.appendChild(el);
    return el;
  }

  var starHost = layer('', 'fx-stars');
  layer('fx-grid');
  layer('fx-vig');
  layer('fx-scan');
  var sweep = layer('fx-sweep');
  var flash = layer('fx-flash');

  var canvas = document.createElement('canvas');
  starHost.appendChild(canvas);
  var ctx = canvas.getContext('2d');

  /* ---------------------------------------------------------- starfield */

  /* Perspective projection: a star is a point in a unit box with a depth, and
     its screen position is that point divided by the depth. Pulling the depth
     down moves it outward, and accelerating that is the whole trick. Drawing
     from the PREVIOUS depth to the current one turns a dot into a streak for
     free, so idle drift and lightspeed are one number apart. */

  var IDLE = 0.0011;
  var WARP = 0.085;
  var stars = [];
  var w = 0, h = 0, cx = 0, cy = 0, dpr = 1;
  var speed = IDLE, target = IDLE;
  var running = false, frame = 0;

  function size() {
    /* Capped at 2: a phone reporting 3 triples the fill cost of a full-screen
       canvas for a difference nobody can see on a starfield. */
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    cx = w / 2;
    cy = h / 2;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    var want = Math.max(90, Math.min(420, Math.round((w * h) / 5200)));
    while (stars.length > want) stars.pop();
    while (stars.length < want) stars.push(spawn(Math.random()));
  }

  function spawn(z) {
    return {
      x: (Math.random() - 0.5) * 2,
      y: (Math.random() - 0.5) * 2,
      z: z <= 0 ? 1 : z,
      pz: 0,
      m: Math.random() < 0.08 ? 1.9 : 1
    };
  }

  function tick() {
    frame = 0;
    if (!running) return;

    speed += (target - speed) * 0.055;
    var warp = Math.max(0, Math.min(1, (speed - IDLE) / (WARP - IDLE)));

    /* Trails rather than a clear: the fade leaves the previous frame faintly
       behind, which is what makes streaks look like light. */
    ctx.fillStyle = 'rgba(11,14,18,' + (0.72 - warp * 0.42).toFixed(3) + ')';
    ctx.fillRect(0, 0, w, h);
    ctx.lineCap = 'round';

    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      s.pz = s.z;
      s.z -= speed;
      if (s.z <= 0.02) { stars[i] = spawn(1); continue; }

      var k = Math.min(cx, cy) * 1.35;
      var x = cx + (s.x / s.z) * k;
      var y = cy + (s.y / s.z) * k;
      if (x < -60 || x > w + 60 || y < -60 || y > h + 60) { stars[i] = spawn(1); continue; }

      var px = cx + (s.x / s.pz) * k;
      var py = cy + (s.y / s.pz) * k;

      var depth = 1 - s.z;
      var alpha = Math.min(1, depth * depth * 1.5) * (0.35 + warp * 0.65) * s.m;

      ctx.strokeStyle = warp > 0.04
        ? 'rgba(' + Math.round(196 + warp * 59) + ',' +
                    Math.round(232 + warp * 23) + ',255,' + alpha.toFixed(3) + ')'
        : 'rgba(150,196,214,' + (alpha * 0.72).toFixed(3) + ')';
      ctx.lineWidth = Math.max(0.5, depth * 1.7 * s.m);
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(x, y + (px === x && py === y ? 0.6 : 0));
      ctx.stroke();
    }

    frame = window.requestAnimationFrame(tick);
  }

  function start() {
    if (running || document.hidden) return;
    running = true;
    frame = window.requestAnimationFrame(tick);
  }

  function stop() {
    running = false;
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
  }

  function warpTo(level) { target = IDLE + (WARP - IDLE) * level; }

  size();
  start();

  var resizeTimer = 0;
  window.addEventListener('resize', function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(size, 150);
  });
  /* A background tab burning a rAF loop on a starfield nobody is looking at is
     the cost this layer has to justify, so it does not pay it. */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });

  /* ------------------------------------------- panel materialisation */

  /* `.demo-banner` is deliberately absent from this list. It is the one element
     on the site that must be visible the instant the page paints, because it is
     the label saying every figure here is invented. */
  var PANELS = '.page-head,.card,.chart-frame,.callout,.table-wrap,.yaml,' +
               '.signin-card,section.block > h2,.result-count';

  function finish(el) {
    el.classList.remove('fx-in');
    el.classList.add('fx-done');
  }

  /* Queries the document rather than trusting a list built earlier: this is the
     function that guarantees no figure is left hidden, so it must not depend on
     any of the code between here and the failure it is catching. */
  function settleAll() {
    var all = document.querySelectorAll('.fx-panel');
    for (var i = 0; i < all.length; i++) finish(all[i]);
  }

  /* Armed before anything else can throw.

     It is re-armed rather than cancelled once setup succeeds, and the two
     durations do different jobs. The short one catches a throw between hiding
     and playing, which is the case that would leave a figure invisible with
     nothing coming to fix it. The long one is the last resort for panels the
     observer owns: settling those after two and a half seconds would force
     everything below the fold visible before anyone scrolled to it, so the
     reveal-on-scroll could never happen at all. */
  var guard = window.setTimeout(settleAll, 2600);

  function rearm(ms) {
    window.clearTimeout(guard);
    guard = window.setTimeout(settleAll, ms);
  }

  function play(list) {
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      if (el.classList.contains('fx-in') || el.classList.contains('fx-done')) continue;
      el.style.setProperty('--fx-d', Math.min(i * 45, 420) + 'ms');
      el.classList.add('fx-in');
      el.addEventListener('animationend', function (e) {
        if (e.animationName === 'fx-rise') finish(e.currentTarget);
      }, { once: true });
    }
  }

  function materialise() {
    var panels = Array.prototype.slice.call(document.querySelectorAll(PANELS));
    if (!panels.length) return;

    /* The whole hiding step in one synchronous block: mark the elements, set
       the root flag, and hand every one of them to something that shows it
       again. Nothing that can throw may be added between these lines. */
    var above = [], below = [];
    for (var i = 0; i < panels.length; i++) {
      panels[i].classList.add('fx-panel');
      (panels[i].getBoundingClientRect().top < window.innerHeight + 80 ? above : below)
        .push(panels[i]);
    }
    doc.classList.add('fx-ready');
    play(above);

    if (!below.length) { rearm(3000); return; }
    if ('IntersectionObserver' in window) {
      var io = new IntersectionObserver(function (entries) {
        var batch = [];
        for (var j = 0; j < entries.length; j++) {
          if (entries[j].isIntersecting) {
            batch.push(entries[j].target);
            io.unobserve(entries[j].target);
          }
        }
        if (batch.length) play(batch);
      }, { rootMargin: '0px 0px -8% 0px' });
      for (var k = 0; k < below.length; k++) io.observe(below[k]);
      /* Setup succeeded, so the observer owns these. Push the backstop out far
         enough that scrolling to a panel is what reveals it, and never remove
         it: a page left open still ends up wholly visible. */
      rearm(20000);
    } else {
      play(below);
      rearm(3000);
    }
  }

  /* --------------------------------------------------------------- jump */

  var jumping = false;

  function jump(href) {
    if (jumping) return;
    jumping = true;
    warpTo(1);
    flash.classList.add('out');
    store.set('mudhorn.jump', '1');
    window.setTimeout(function () { window.location.href = href; }, 500);
    /* If the navigation never happens the deck comes back, rather than sitting
       behind a white screen at lightspeed forever. */
    window.setTimeout(function () {
      jumping = false;
      warpTo(0);
      flash.classList.remove('out');
    }, 2600);
  }

  function arrive() {
    if (store.get('mudhorn.jump') === '1') {
      store.drop('mudhorn.jump');
      /* Drop OUT of lightspeed: the streaks are already long on the first frame
         and shorten, which is the arrival half of the jump the previous page
         began. */
      speed = WARP;
      flash.classList.add('in');
      sweep.classList.add('run');
    }
    warpTo(0);
  }

  /* Same-origin link clicks become jumps. Everything else a browser can do with
     a link is left alone: a new tab, a download, a modified click and an
     external host all behave exactly as they would without this file. */
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey ||
        e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest('a') : null;
    if (!a || !a.href || a.target === '_blank' || a.hasAttribute('download')) return;
    if (a.origin !== window.location.origin) return;
    if (a.getAttribute('href').charAt(0) === '#') return;
    if (a.pathname === window.location.pathname && a.search === window.location.search) return;
    e.preventDefault();
    jump(a.href);
  });

  /* ------------------------------------------------------ boot readout */

  /* Once per browser session. Every line names a piece of this interface and
     not one of them reports a figure: every number on this site is invented,
     and a boot screen implying a live link would undercut the banner three
     lines above it. */
  function boot(then) {
    if (store.get('mudhorn.booted') === '1') { then(); return; }
    store.set('mudhorn.booted', '1');

    var lines = [
      ['Nav computer', 'READY'],
      ['Deck projection', 'ONLINE'],
      ['Render pipeline', 'LOCKED'],
      ['Data source', 'FIXTURE']
    ];

    var el = document.createElement('div');
    el.className = 'fx-boot';
    el.setAttribute('aria-hidden', 'true');
    var items = '';
    for (var i = 0; i < lines.length; i++) {
      items += '<li><span>' + lines[i][0] + '</span><b>' + lines[i][1] + '</b></li>';
    }
    el.innerHTML = '<div class="panel"><div class="sig">MUDHORN <span>CAPITAL</span></div>' +
                   '<div class="rule"><i></i></div><ul>' + items + '</ul></div>';
    body.appendChild(el);
    warpTo(0.25);

    var li = el.querySelectorAll('li');
    for (var j = 0; j < li.length; j++) {
      (function (node, n) {
        window.setTimeout(function () { node.classList.add('on'); }, 260 + n * 190);
      })(li[j], j);
    }

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      el.classList.add('done');
      speed = WARP * 0.8;
      flash.classList.add('in');
      sweep.classList.add('run');
      window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 460);
      then();
    }
    /* Skippable, and only shown once a session. Nobody wants to watch a title
       sequence twice. */
    el.addEventListener('click', close);
    document.addEventListener('keydown', close, { once: true });
    window.setTimeout(close, 1500);
  }

  function go() {
    boot(function () {
      arrive();
      materialise();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', go);
  } else {
    go();
  }

  window.MUDHORN_FX = { jump: jump, warpTo: warpTo, settle: settleAll };
})();
