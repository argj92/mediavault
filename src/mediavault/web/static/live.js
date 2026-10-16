/* Page behaviour: section navigation, remembered open/closed sections, and
   live status. The page polls /api/ui-state with a plain fetch instead of
   reloading, so open sections, scroll position and half-typed forms survive;
   when the *content* behind them changes (a scan finished, counts moved) it
   offers a reload rather than yanking the page. */
(function () {
  'use strict';

  // ---- a flash message arrives as ?msg=...; keep it out of the address bar so a
  // refresh or a shared link doesn't replay it
  if (location.search.indexOf('msg=') !== -1) {
    var q = new URLSearchParams(location.search); q.delete('msg');
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  }

  // ---- remembered section state (best-effort: storage can be unavailable)
  var KEY = 'mv.sections';
  function loadState() { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } }
  function saveState(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* private mode etc. */ } }

  var sections = Array.prototype.slice.call(document.querySelectorAll('details.section'));
  var remembered = loadState();
  sections.forEach(function (d) {
    if (d.id in remembered) d.open = remembered[d.id];
    d.addEventListener('toggle', function () {
      var s = loadState(); s[d.id] = d.open; saveState(s);
    });
  });

  // ---- navigation: open + scroll to a section; works for #hash on load too
  function goTo(id, push) {
    var d = document.getElementById(id);
    if (!d) return false;
    if (d.tagName === 'DETAILS') d.open = true;
    d.scrollIntoView({behavior: 'smooth', block: 'start'});
    if (push) history.replaceState(null, '', '/#' + id);
    return true;
  }
  document.querySelectorAll('a[href^="/#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      if (location.pathname !== '/') return;          // other pages: normal navigation
      var id = a.getAttribute('href').slice(2);
      if (goTo(id, true)) e.preventDefault();
    });
  });
  if (location.hash.length > 1) {
    // the flash redirect lands on e.g. #duplicates, which may be collapsed
    var target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target && target.tagName === 'DETAILS') {
      target.open = true;
      // the browser's own hash-scroll ran before sections were expanded, so
      // it lands short; redo it now and again once everything has laid out
      target.scrollIntoView();
      window.addEventListener('load', function () { target.scrollIntoView(); });
    }
  }

  // ---- highlight the section being read
  var links = {};
  document.querySelectorAll('.nav a[data-section]').forEach(function (a) { links[a.dataset.section] = a; });
  if ('IntersectionObserver' in window && sections.length) {
    var visible = {};
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { visible[en.target.id] = en.isIntersecting; });
      var first = sections.find(function (d) { return visible[d.id]; });
      Object.keys(links).forEach(function (id) { links[id].classList.toggle('active', !!first && first.id === id); });
    }, {rootMargin: '-10% 0px -70% 0px'});
    sections.forEach(function (d) { io.observe(d); });
  }

  // ---- live status
  var indicator = document.getElementById('live-indicator');
  var liveText = document.getElementById('live-text');
  var banner = document.getElementById('update-banner');
  var ATTN = {}; // key -> [singular, plural]; read from the server-rendered chips
  document.querySelectorAll('[data-attn-chip]').forEach(function (c) { ATTN[c.dataset.attnChip] = c; });
  var ATTN_WORDS = {
    duplicates: ['duplicate group', 'duplicate groups'],
    sync: ['root out of sync', 'roots out of sync'],
    promote: ['file to promote', 'files to promote'],
    suggestions: ['new file found', 'new files found'],
    peers: ['pairing request', 'pairing requests']
  };

  function setCount(key, n) {
    var badge = document.querySelector('[data-count="' + key + '"]');
    if (badge) { badge.textContent = n || ''; badge.hidden = !n; }
    var chip = ATTN[key];
    if (chip) {
      chip.hidden = !n;
      chip.querySelector('b').textContent = n;
      var w = ATTN_WORDS[key];
      chip.querySelector('.chip-text').textContent = n === 1 ? w[0] : w[1];
    }
  }

  function render(data) {
    data.roots.forEach(function (r) {
      var card = document.querySelector('.root-card[data-root="' + CSS.escape(r.label) + '"]');
      if (card) {
        card.className = 'root-card ' + r.state;
        var t = card.querySelector('.js-root-text');
        t.textContent = r.text; t.title = r.detail || '';
      }
      var chip = document.querySelector('[data-root-chip="' + CSS.escape(r.label) + '"]');
      if (chip) {
        chip.className = 'chip ' + r.state;
        chip.querySelector('.chip-text').textContent = r.short;
      }
    });
    if (data.counts) Object.keys(data.counts).forEach(function (k) { if (k !== 'library' && k !== 'roots') setCount(k, data.counts[k]); });
  }

  // What the page content depends on: finished scans and the counts. If it
  // differs from what the first poll saw, the sections on screen are stale.
  function signature(data) {
    return JSON.stringify([
      data.roots.map(function (r) { return [r.label, r.scanned_at, r.state === 'scanning']; }),
      data.counts || null
    ]);
  }

  var baseline = null, lastCounts = null, polls = 0, failures = 0, timer = null;

  function schedule(data) {
    var scanning = data && data.any_scan_active;
    var wait = failures ? Math.min(30000, 3000 * Math.pow(2, failures)) : (scanning ? 3000 : 15000);
    clearTimeout(timer);
    timer = setTimeout(poll, wait);
  }

  function poll() {
    if (document.hidden) { schedule(null); return; }
    polls++;
    // While scanning, the (heavier) counts only every 5th poll; idle: every poll.
    var wantCounts = !lastCounts || polls % 5 === 0 || !lastScanning;
    fetch('/api/ui-state?counts=' + (wantCounts ? 1 : 0), {headers: {'Accept': 'application/json'}, cache: 'no-store'})
      .then(function (res) { if (!res.ok) throw new Error(res.status); return res.json(); })
      .then(function (data) {
        failures = 0;
        if (!data.counts && lastCounts) data.counts = lastCounts; else if (data.counts) lastCounts = data.counts;
        lastScanning = data.any_scan_active;
        render(data);
        var sig = signature(data);
        if (baseline === null) baseline = sig;
        else if (sig !== baseline && banner) banner.hidden = false;
        indicator.classList.remove('down'); liveText.textContent = data.any_scan_active ? 'scanning…' : 'live';
        schedule(data);
      })
      .catch(function () {
        failures++;
        indicator.classList.add('down'); liveText.textContent = 'reconnecting…';
        schedule(null);
      });
  }
  var lastScanning = false;
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { clearTimeout(timer); poll(); } });
  poll();
})();
