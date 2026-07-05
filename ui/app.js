/* DriftCheck local UI — no framework, no build.  */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt !== undefined) e.textContent = txt; return e; };

  // ------- THEME -------
  (function initTheme(){
    const KEY = 'driftcheck-theme';
    const root = document.documentElement;
    const stored = (() => { try { return localStorage.getItem(KEY); } catch { return null; } })();
    const initial = (stored === 'light' || stored === 'dark') ? stored
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    root.setAttribute('data-theme', initial);
    document.addEventListener('DOMContentLoaded', () => {
      const btn = $('themeToggle');
      if (!btn) return;
      btn.addEventListener('click', () => {
        const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', next);
        try { localStorage.setItem(KEY, next); } catch {}
      });
    });
  })();

  // ------- APP STATE -------
  const state = {
    connections: [],   // [{name, provider, model}]
    tests: [],         // [{name, prompt, ...}]
    lastResults: null, // { test, connections, results: [...] }
  };

  // ------- API -------
  async function api(path, opts) {
    const res = await fetch(path, opts);
    const json = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = json.error || {};
      throw new Error(err.message || `${res.status} ${res.statusText}`);
    }
    return json;
  }

  function toast(msg, cls = 'ok') {
    const t = el('div', `toast ${cls}`, msg);
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => {
      t.classList.remove('show');
      setTimeout(() => t.remove(), 300);
    }, 4200);
  }

  // ------- FORM: TEST PICKER (checkbox list) -------
  function renderTestList() {
    const box = $('testList');
    box.innerHTML = '';
    if (!state.tests.length) {
      box.appendChild(el('em', 'dim', 'No tests defined in settings/config.yaml.'));
      return;
    }
    state.tests.forEach((t, i) => {
      const wrap = el('label', 'model-item');
      const cb = el('input'); cb.type = 'checkbox'; cb.value = t.name;
      if (i === 0) { cb.checked = true; wrap.classList.add('checked'); }
      cb.addEventListener('change', () => {
        wrap.classList.toggle('checked', cb.checked);
        onTestSelectionChange(t.name);
      });
      wrap.addEventListener('click', (ev) => {
        // Clicking anywhere on the item previews its prompt (without stealing focus)
        if (ev.target !== cb) previewTest(t.name);
      });
      const body = el('div', 'm-body');
      body.appendChild(el('span', 'm-name', t.name));
      const meta = [];
      if (t.repeats)         meta.push(`${t.repeats}×`);
      if (t.criterion)       meta.push('criterion');
      if (t.reference_file)  meta.push('RAG');
      if (t.filler_turns)    meta.push(`${t.filler_turns} filler`);
      if (t.test_assentation) meta.push('assentation');
      body.appendChild(el('span', 'm-model', meta.join(' · ') || t.prompt_file));
      wrap.appendChild(cb); wrap.appendChild(body);
      box.appendChild(wrap);
    });
    // preview the first (auto-checked) test
    previewTest(state.tests[0].name);
    updateTestCount();
  }

  function selectedTests() {
    return Array.from(document.querySelectorAll('#testList input:checked')).map(cb => cb.value);
  }

  function setTestSelection(pred) {
    document.querySelectorAll('#testList .model-item').forEach(item => {
      const cb = item.querySelector('input');
      const t = state.tests.find(x => x.name === cb.value);
      const on = pred ? pred(t) : false;
      cb.checked = on;
      item.classList.toggle('checked', on);
    });
    updateTestCount();
  }

  function updateTestCount() {
    const badge = $('testCountBadge');
    const n = selectedTests().length;
    const total = state.tests.length;
    badge.textContent = `${n} of ${total} selected`;
  }

  function onTestSelectionChange(lastClickedName) {
    updateTestCount();
    // update the prompt preview if the just-toggled test is now checked,
    // otherwise fall back to the first checked test.
    const selected = selectedTests();
    if (selected.includes(lastClickedName)) previewTest(lastClickedName);
    else if (selected.length) previewTest(selected[0]);
    else $('promptPreview').value = '(no test selected)';
  }

  function previewTest(name) {
    const t = state.tests.find(x => x.name === name);
    if (!t) return;
    $('promptPreview').value = t.prompt || '(prompt file empty)';
    // On preview change, prefill overrides with the test's own defaults
    if (t.repeats != null)      $('repeatsInput').value = t.repeats;
    if (t.temperature != null)  $('temperatureInput').value = t.temperature;
    if (t.filler_turns != null) $('fillerInput').value = t.filler_turns;
    $('assentationInput').checked = !!t.test_assentation;
  }

  function wireTestPicks() {
    $('pickAllTests').addEventListener('click', () => setTestSelection(() => true));
    $('pickNoTests').addEventListener('click',  () => setTestSelection(() => false));
  }

  // ------- FORM: MODEL LIST -------
  function renderModelList() {
    const box = $('modelList');
    box.innerHTML = '';
    if (!state.connections.length) {
      box.appendChild(el('em', 'dim', 'No connections defined in settings/config.yaml.'));
      return;
    }
    state.connections.forEach(c => {
      const wrap = el('label', 'model-item');
      const cb = el('input'); cb.type = 'checkbox'; cb.value = c.name;
      cb.addEventListener('change', () => {
        wrap.classList.toggle('checked', cb.checked);
        updateTemperatureHint();
      });
      const body = el('div', 'm-body');
      const nameRow = el('span', 'm-name');
      nameRow.textContent = c.name;
      if (c.is_reasoning) {
        const pill = el('span', 'm-reason-pill', 'reasoning');
        pill.title = 'This model ignores custom temperature.';
        nameRow.appendChild(pill);
      }
      body.appendChild(nameRow);
      body.appendChild(el('span', 'm-model', c.model || ''));
      const badge = el('span', `m-badge ${c.provider || ''}`, c.provider || '');
      wrap.appendChild(cb); wrap.appendChild(body); wrap.appendChild(badge);
      box.appendChild(wrap);
    });
  }

  function updateTemperatureHint() {
    const hint = $('tempHint');
    const input = $('temperatureInput');
    const selected = selectedConnections();
    const reasoningSelected = selected
      .map(name => state.connections.find(c => c.name === name))
      .filter(c => c && c.is_reasoning);
    if (reasoningSelected.length === 0) {
      hint.hidden = true;
      input.disabled = false;
      return;
    }
    const names = reasoningSelected.map(c => c.name).join(', ');
    if (reasoningSelected.length === selected.length) {
      hint.hidden = false;
      hint.textContent = `⚠ Temperature is ignored — all selected models are reasoning models (${names}).`;
      input.disabled = true;
    } else {
      hint.hidden = false;
      hint.textContent = `⚠ Temperature is ignored for reasoning models: ${names}. Other models will use ${input.value}.`;
      input.disabled = false;
    }
  }

  function selectedConnections() {
    return Array.from(document.querySelectorAll('#modelList input:checked')).map(cb => cb.value);
  }
  function setSelection(pred) {
    document.querySelectorAll('#modelList .model-item').forEach(item => {
      const cb = item.querySelector('input');
      const name = cb.value;
      const conn = state.connections.find(c => c.name === name);
      const on = pred ? pred(conn) : false;
      cb.checked = on;
      item.classList.toggle('checked', on);
    });
    updateTemperatureHint();
  }
  function wireModelPicks() {
    $('pickAll').addEventListener('click',   () => setSelection(() => true));
    $('pickNone').addEventListener('click',  () => setSelection(() => false));
    $('pickAnthropic').addEventListener('click', () => setSelection(c => c && c.provider === 'anthropic'));
    $('pickOpenAI').addEventListener('click',    () => setSelection(c => c && c.provider === 'openai'));
    $('pickGoogle').addEventListener('click',    () => setSelection(c => c && c.provider === 'google'));
  }

  // ------- RUN -------
  async function onRun() {
    const tests = selectedTests();
    const conns = selectedConnections();
    if (!tests.length) {
      toast('Pick at least one test.', 'err');
      return;
    }
    if (!conns.length) {
      toast('Pick at least one model.', 'err');
      return;
    }
    const overrides = {
      repeats: parseInt($('repeatsInput').value, 10) || undefined,
      temperature: parseFloat($('temperatureInput').value),
      filler_turns: parseInt($('fillerInput').value, 10) || 0,
      test_assentation: $('assentationInput').checked,
    };
    const total = tests.length * conns.length;
    const btn = $('runBtn');
    const note = $('runNote');
    btn.disabled = true;
    btn.textContent = 'Running…';
    note.textContent = `Running ${tests.length} test(s) × ${conns.length} model(s) = ${total} run(s) × ${overrides.repeats} pass. May take a while.`;
    const t0 = performance.now();
    try {
      const resp = await api('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tests, connections: conns, overrides }),
      });
      state.lastResults = { tests, connections: conns, results: resp.results };
      renderResults(state.lastResults);
      const dt = ((performance.now() - t0) / 1000).toFixed(1);
      const nErr = resp.results.reduce((s, r) => s + (r.summary?.n_errors || 0) + (r.error ? 1 : 0), 0);
      toast(`Done in ${dt}s${nErr ? ` — with ${nErr} error(s)` : ''}.`, nErr ? 'err' : 'ok');
      loadHistory();
    } catch (e) {
      toast(`Run failed: ${e.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run tests';
      note.textContent = '';
    }
  }

  // ------- RENDER RESULTS -------
  const METRICS = [
    { key: 'consistency',            label: 'Consistency',            hint: 'higher = same answer each time' },
    { key: 'criterion_pass_rate',    label: 'Criterion pass rate',    hint: 'regex-based check' },
    { key: 'assentation_flip_rate',  label: 'Assentation flip rate',  hint: 'lower = didn\'t cave under pushback', invert: true },
    { key: 'faithfulness',           label: 'Faithfulness',           hint: 'answer grounded in reference doc' },
  ];

  function providerColor(prov) {
    switch ((prov || '').toLowerCase()) {
      case 'anthropic': return 'var(--clr-anthropic)';
      case 'openai':    return 'var(--clr-openai)';
      case 'google':    return 'var(--clr-google)';
      default:          return 'var(--clr-local)';
    }
  }

  function renderResults(data) {
    const card = $('resultsCard');
    card.hidden = false;
    const body = $('resultsBody');
    body.innerHTML = '';

    // Group results by test name, preserving encounter order
    const byTest = new Map();
    data.results.forEach(r => {
      if (!byTest.has(r.test)) byTest.set(r.test, []);
      byTest.get(r.test).push(r);
    });

    $('resultsTitle').textContent = byTest.size === 1
      ? [...byTest.keys()][0]
      : `${byTest.size} tests`;
    $('resultsSubtitle').textContent = `${data.results.length} run(s) · ${new Date().toLocaleString()}`;

    byTest.forEach((rows, testName) => {
      body.appendChild(renderTestGroup(testName, rows));
    });

    $('downloadAll').onclick = () => {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `driftcheck_run_${Date.now()}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };

    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderTestGroup(testName, rows) {
    const wrap = el('section', 'test-group');
    const head = el('div', 'test-group-head');
    head.appendChild(el('h3', 'test-group-title', testName));
    const nErr = rows.reduce((s, r) => s + (r.summary?.n_errors || 0) + (r.error ? 1 : 0), 0);
    head.appendChild(el('span', 'test-group-meta',
      `${rows.length} model(s) · ${nErr ? nErr + ' error(s)' : 'clean run'}`));
    wrap.appendChild(head);

    // Bar chart
    const barBlock = el('div', 'metric-block');
    barBlock.appendChild(el('h4', 'metric-block-title', 'Model comparison'));
    const chartBody = el('div', 'chart-body');
    renderBarsInto(chartBody, rows);
    barBlock.appendChild(chartBody);
    wrap.appendChild(barBlock);

    // Summary cards
    const cardsGrid = el('div', 'cards-grid');
    renderSummaryCardsInto(cardsGrid, rows);
    wrap.appendChild(cardsGrid);

    // Radar (if ≥ 2 models)
    if (rows.length >= 2) {
      const radarBlock = el('div', 'metric-block');
      radarBlock.appendChild(el('h4', 'metric-block-title', 'Radar profile'));
      const rw = el('div', 'radar-wrap');
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'radar');
      svg.setAttribute('viewBox', '0 0 400 400');
      svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      rw.appendChild(svg);
      radarBlock.appendChild(rw);
      const legend = el('div', 'chart-legend');
      radarBlock.appendChild(legend);
      renderRadarInto(svg, legend, rows);
      wrap.appendChild(radarBlock);
    }

    // Raw answers
    const answersBlock = el('div', 'metric-block');
    answersBlock.appendChild(el('h4', 'metric-block-title', 'Raw answers'));
    const ab = el('div', 'answers-block');
    renderAnswersInto(ab, rows);
    answersBlock.appendChild(ab);
    wrap.appendChild(answersBlock);

    return wrap;
  }

  function renderBarsInto(body, results) {
    METRICS.forEach(m => {
      const grp = el('div', 'metric-group');
      const head = el('div', 'metric-head');
      head.appendChild(el('span', 'metric-name', m.label));
      head.appendChild(el('span', `metric-hint${m.invert ? ' invert' : ''}`, m.hint));
      grp.appendChild(head);
      results.forEach((r, i) => {
        const row = el('div', 'metric-bar');
        row.appendChild(el('span', 'bar-model', r.connection));
        const track = el('div', 'bar-track');
        const val = r.summary ? r.summary[m.key] : null;
        if (val == null) {
          track.classList.add('na');
        } else {
          const fill = el('div', 'bar-fill');
          fill.style.width = `${Math.round(val * 100)}%`;
          fill.style.background = providerColor(r.provider);
          fill.style.animationDelay = `${i * 60}ms`;
          track.appendChild(fill);
        }
        row.appendChild(track);
        row.appendChild(el('span', 'bar-value', val == null ? '—' : `${(val * 100).toFixed(1)}%`));
        grp.appendChild(row);
      });
      body.appendChild(grp);
    });
  }

  function renderSummaryCardsInto(box, results) {
    results.forEach(r => {
      const card = el('div', `result-card${r.error ? ' err' : ''}`);
      const h = el('h4', null, r.connection);
      card.appendChild(h);
      card.appendChild(el('div', 'rc-model', `${r.provider || '?'}/${r.model || '?'}`));
      const s = r.summary || {};
      METRICS.forEach(m => {
        const line = el('div', 'metric');
        line.appendChild(el('span', null, m.label));
        line.appendChild(el('span', null, s[m.key] == null ? '—' : `${(s[m.key] * 100).toFixed(1)}%`));
        card.appendChild(line);
      });
      const ok = s.n_answers != null ? s.n_answers : 0;
      const err = s.n_errors != null ? s.n_errors : 0;
      const count = el('div', 'metric');
      count.appendChild(el('span', null, 'runs'));
      count.appendChild(el('span', null, `${ok} ok / ${err} err`));
      card.appendChild(count);
      if (r.error) card.appendChild(el('div', 'err-note', r.error));
      box.appendChild(card);
    });
  }

  function renderRadarInto(svg, legend, results) {
    const NS = 'http://www.w3.org/2000/svg';
    const cx = 200, cy = 200, R = 130;
    const g = document.createElementNS(NS, 'g'); g.setAttribute('class', 'radar-grid');
    [0.25, 0.5, 0.75, 1].forEach(f => {
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.setAttribute('r', R * f);
      g.appendChild(c);
    });
    svg.appendChild(g);
    const axg = document.createElementNS(NS, 'g'); axg.setAttribute('class', 'radar-axis-lines');
    const l1 = document.createElementNS(NS, 'line'); l1.setAttribute('x1', cx); l1.setAttribute('y1', cy - R); l1.setAttribute('x2', cx); l1.setAttribute('y2', cy + R);
    const l2 = document.createElementNS(NS, 'line'); l2.setAttribute('x1', cx - R); l2.setAttribute('y1', cy); l2.setAttribute('x2', cx + R); l2.setAttribute('y2', cy);
    axg.appendChild(l1); axg.appendChild(l2);
    svg.appendChild(axg);
    const lg = document.createElementNS(NS, 'g'); lg.setAttribute('class', 'radar-axis');
    function label(x, y, anchor, text){
      const t = document.createElementNS(NS, 'text');
      t.setAttribute('x', x); t.setAttribute('y', y);
      t.setAttribute('text-anchor', anchor); t.textContent = text;
      lg.appendChild(t);
    }
    label(cx, cy - R - 12, 'middle', 'Consistency');
    label(cx + R + 12, cy + 4, 'start',  'Criterion');
    label(cx, cy + R + 22, 'middle', '¬ Assentation');
    label(cx - R - 12, cy + 4, 'end',   'Faithfulness');
    svg.appendChild(lg);
    results.forEach((r, i) => {
      const s = r.summary || {};
      const cV = s.consistency ?? 0;
      const kV = s.criterion_pass_rate ?? 0;
      const aV = s.assentation_flip_rate == null ? 0 : (1 - s.assentation_flip_rate);
      const fV = s.faithfulness ?? 0;
      const pts = [
        [cx,           cy - R * cV],
        [cx + R * kV,  cy],
        [cx,           cy + R * aV],
        [cx - R * fV,  cy],
      ].map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
      const poly = document.createElementNS(NS, 'polygon');
      poly.setAttribute('class', 'radar-poly');
      poly.setAttribute('points', pts);
      const c = providerColor(r.provider);
      poly.style.fill = c; poly.style.stroke = c;
      poly.style.animation = `radarIn .8s ease both`;
      poly.style.animationDelay = `${i * 100}ms`;
      svg.appendChild(poly);
    });
    results.forEach(r => {
      const it = el('span', 'chart-legend-item');
      const sw = el('span', 'legend-swatch');
      sw.style.background = providerColor(r.provider);
      it.appendChild(sw);
      it.appendChild(el('span', null, r.connection));
      legend.appendChild(it);
    });
  }

  function renderAnswersInto(box, results) {
    results.forEach(r => {
      const d = el('details');
      const s = el('summary');
      s.appendChild(el('span', null, r.connection));
      s.appendChild(el('span', 'dim', `${(r.answers || []).length} answer(s)`));
      d.appendChild(s);
      const body = el('div', 'answer-body');
      (r.answers || []).forEach((ans, i) => {
        const it = el('div', 'answer-item');
        it.appendChild(el('div', 'answer-idx', `pass #${i + 1}`));
        it.appendChild(el('div', 'answer-text', ans));
        if (r.pushback_pairs && r.pushback_pairs[i]) {
          const pb = el('div', 'pushback');
          pb.appendChild(el('div', 'answer-text', r.pushback_pairs[i][1]));
          it.appendChild(pb);
        }
        body.appendChild(it);
      });
      if (!(r.answers || []).length && r.error) {
        body.appendChild(el('div', 'err-note', r.error));
      }
      d.appendChild(body);
      box.appendChild(d);
    });
  }

  // ------- HISTORY -------
  async function loadHistory() {
    const box = $('historyList');
    box.innerHTML = '<em class="dim">Loading…</em>';
    try {
      const items = await api('/api/results');
      if (!items.length) { box.innerHTML = '<em class="dim">No runs yet. Try running a test above.</em>'; return; }
      box.innerHTML = '';
      items.forEach(it => {
        const row = el('div', 'history-row');
        row.appendChild(el('span', 'h-time', new Date((it.mtime || 0) * 1000).toLocaleString()));
        row.appendChild(el('span', 'h-test', it.test || '—'));
        row.appendChild(el('span', 'h-model', `${it.provider || '?'}/${it.model || '?'}`));
        const pct = (v) => v == null ? '—' : `${(v * 100).toFixed(0)}%`;
        row.appendChild(el('span', 'h-metric', pct(it.summary?.consistency)));
        row.appendChild(el('span', 'h-metric', pct(it.summary?.criterion_pass_rate)));
        row.appendChild(el('span', 'h-metric', pct(it.summary?.assentation_flip_rate)));
        row.appendChild(el('span', 'h-metric', pct(it.summary?.faithfulness)));
        row.appendChild(el('span', 'h-count', `${it.summary?.n_answers ?? 0}/${it.summary?.n_errors ?? 0}`));
        row.title = 'Click to load this result';
        row.addEventListener('click', () => loadHistoryEntry(it.file));
        box.appendChild(row);
      });
    } catch (e) {
      box.innerHTML = `<em class="dim">Failed to load history: ${e.message}</em>`;
    }
  }
  async function loadHistoryEntry(file) {
    try {
      const one = await api(`/api/results/${encodeURIComponent(file)}`);
      state.lastResults = { test: one.test, connections: [one.connection], results: [one] };
      renderResults(state.lastResults);
    } catch (e) {
      toast(`Could not load ${file}: ${e.message}`, 'err');
    }
  }

  // ------- BOOT -------
  document.addEventListener('DOMContentLoaded', async () => {
    const status = $('status');
    try {
      const cfg = await api('/api/config');
      state.connections = cfg.connections || [];
      state.tests = cfg.tests || [];
      status.textContent = `${state.connections.length} connections · ${state.tests.length} tests`;
      status.classList.add('ok');
      renderTestList();
      wireTestPicks();
      renderModelList();
      wireModelPicks();
      $('runBtn').addEventListener('click', onRun);
      $('runBtn').textContent = 'Run tests';
      $('refreshHistory').addEventListener('click', loadHistory);
      loadHistory();
    } catch (e) {
      status.textContent = 'API error';
      status.classList.add('err');
      toast(`Failed to load config: ${e.message}`, 'err');
    }
  });

  // Small keyframe injected via JS so the radar animation lives in one place
  const s = document.createElement('style');
  s.textContent = `
    @keyframes radarIn { from { opacity:0; transform:scale(.9); transform-origin:center; }
                         to   { opacity:1; transform:scale(1); } }
  `;
  document.head.appendChild(s);
})();
