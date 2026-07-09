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

  // ------- PROGRESS POLLING -------
  // Polls GET /api/progress while a run/evaluate is active and updates the
  // button's label with a live percentage, e.g. "Running…23%". Run and
  // Evaluate are tracked independently server-side, so both buttons can show
  // their own live percentage even if both operations are active at once.
  function startProgressPoll(kind, labelEl, baseLabel) {
    labelEl.textContent = baseLabel + '…';
    const iv = setInterval(async () => {
      try {
        const snap = await api('/api/progress');
        const s = snap && snap[kind];
        if (s && s.active && typeof s.percent === 'number') {
          labelEl.textContent = `${baseLabel}…${s.percent}%`;
        }
      } catch { /* ignore transient polling errors */ }
    }, 600);
    return iv;
  }
  function stopProgressPoll(iv, labelEl, baseLabel) {
    clearInterval(iv);
    labelEl.textContent = baseLabel;
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
    const btnLabel = $('runBtnLabel');
    const note = $('runNote');
    btn.disabled = true;
    note.textContent = `Running ${tests.length} test(s) × ${conns.length} model(s) = ${total} run(s) × ${overrides.repeats} pass. May take a while.`;
    const t0 = performance.now();
    let reportLinkSet = false;
    const progIv = startProgressPoll('run', btnLabel, 'Running');
    try {
      const resp = await api('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tests, connections: conns, overrides }),
      });

      note.textContent = 'Run finished. Evaluating with QSL…';
      let finalPayload = { tests, connections: conns, results: resp.results };
      try {
        const evaluated = await api('/api/evaluate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ results: resp.results }),
        });
        finalPayload = { ...evaluated, tests, connections: conns };
        if (evaluated._markdown_report) {
          note.innerHTML = `Narrative report saved: <a href="/api/evaluation-report/${encodeURIComponent(evaluated._markdown_report)}" target="_blank" rel="noopener">outputs/evaluation/${evaluated._markdown_report}</a>`;
          reportLinkSet = true;
        }
      } catch (evalErr) {
        toast(`Run finished, but QSL evaluate failed: ${evalErr.message}`, 'err');
      }

      state.lastResults = finalPayload;
      renderResults(state.lastResults);
      const dt = ((performance.now() - t0) / 1000).toFixed(1);
      const nErr = (state.lastResults.results || []).reduce((s, r) => s + (r.summary?.n_errors || 0) + (r.error ? 1 : 0), 0);
      toast(`Done + evaluated in ${dt}s${nErr ? ` — with ${nErr} error(s)` : ''}.`, nErr ? 'err' : 'ok');
      // Results always contain only the latest run.
    } catch (e) {
      toast(`Run failed: ${e.message}`, 'err');
    } finally {
      btn.disabled = false;
      stopProgressPoll(progIv, btnLabel, 'Run tests');
      if (!reportLinkSet) note.textContent = '';
    }
  }

  async function onEvaluateOnly() {
    const tests = selectedTests();
    const conns = selectedConnections();
    if (!tests.length) {
      toast('Pick at least one test to evaluate from outputs/.', 'err');
      return;
    }
    if (!conns.length) {
      toast('Pick at least one model to evaluate from outputs/.', 'err');
      return;
    }

    const evalBtn = $('evaluateOnlyBtn');
    const evalBtnLabel = $('evalBtnLabel');
    const note = $('runNote');
    evalBtn.disabled = true;
    note.textContent = `Evaluating only the newest timestamped run for ${tests.length} selected test(s) × ${conns.length} selected model(s). No older outputs are included.`;
    const t0 = performance.now();
    let reportLinkSet = false;
    const progIv = startProgressPoll('evaluate', evalBtnLabel, 'Evaluate');
    try {
      const evaluated = await api('/api/evaluate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tests, connections: conns, latest_run_only: true }),
      });
      state.lastResults = { ...evaluated, tests, connections: conns };
      renderResults(state.lastResults);
      const dt = ((performance.now() - t0) / 1000).toFixed(1);
      toast(`Evaluate finished in ${dt}s · ${evaluated._loaded_results || 0} output file(s) processed.`, 'ok');
      if (evaluated._markdown_report) {
        note.innerHTML = `Narrative report saved: <a href="/api/evaluation-report/${encodeURIComponent(evaluated._markdown_report)}" target="_blank" rel="noopener">outputs/evaluation/${evaluated._markdown_report}</a>`;
        reportLinkSet = true;
      }
      // Results always contain only the latest evaluated run.
    } catch (e) {
      toast(`Evaluate failed: ${e.message}`, 'err');
    } finally {
      evalBtn.disabled = false;
      stopProgressPoll(progIv, evalBtnLabel, 'Evaluate');
      if (!reportLinkSet) note.textContent = '';
    }
  }

  // ------- RENDER RESULTS -------
  const EVAL_METRICS = [
    { key: 'qsl_score',        label: 'QSL score',        hint: 'overall evaluated score' },
    { key: 'correctness',      label: 'Correctness',      hint: 'matches expected output / criterion' },
    { key: 'grounding',        label: 'Grounding',        hint: 'stays supported by expected/reference' },
    { key: 'no_hallucination', label: 'No hallucination', hint: 'higher = fewer unsupported claims' },
    { key: 'completeness',     label: 'Completeness',     hint: 'covers expected content' },
    { key: 'format_score',     label: 'Format',           hint: 'format / regex compliance' },
  ];

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

  function renderResults(data, opts = {}) {
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
    const evalSummary = data.summary
      ? ` · QSL: ${data.summary.n_pass || 0} pass, ${data.summary.n_partial || 0} partial, ${data.summary.n_drift || 0} drift · evaluator: ${data.summary.rag_model || 'deterministic'} (${data.summary.n_rag_model || 0} model / ${data.summary.n_deterministic || 0} fallback)`
      : '';
    const loadedAt = data._loaded_timestamp
      ? new Date(data._loaded_timestamp * 1000)
      : (data.summary && data.summary.created_at ? new Date(data.summary.created_at) : new Date());
    const sourceLabel = data._loaded_from ? ` · ${data._loaded_from}` : '';
    $('resultsSubtitle').textContent = `${data.results.length} run(s) · ${loadedAt.toLocaleString()}${sourceLabel}${evalSummary}`;

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

    const collapseBtn = $('resultsCollapseBtn');
    body.hidden = false;
    collapseBtn.textContent = '▾ Collapse';
    collapseBtn.onclick = () => {
      body.hidden = !body.hidden;
      collapseBtn.textContent = body.hidden ? '▸ Expand' : '▾ Collapse';
    };

    if (opts.scroll !== false) {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function renderTestGroup(testName, rows) {
    const wrap = el('section', 'test-group');
    const head = el('div', 'test-group-head');
    head.appendChild(el('h3', 'test-group-title', testName));
    const nErr = rows.reduce((s, r) => s + (r.summary?.n_errors || 0) + (r.error ? 1 : 0), 0);
    head.appendChild(el('span', 'test-group-meta',
      `${rows.length} model(s) · ${nErr ? nErr + ' error(s)' : 'clean run'}`));
    wrap.appendChild(head);

    // QSL evaluation block
    if (rows.some(r => r.summary && r.summary.qsl_score != null)) {
      const evalBlock = el('div', 'metric-block');
      evalBlock.appendChild(el('h4', 'metric-block-title', 'QSL evaluation'));
      const evalBody = el('div', 'chart-body');
      renderEvalBarsInto(evalBody, rows);
      evalBlock.appendChild(evalBody);
      const recs = el('div', 'recommendations-block');
      renderRecommendationsInto(recs, rows);
      evalBlock.appendChild(recs);
      wrap.appendChild(evalBlock);
    }

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

    // Raw answers
    const answersBlock = el('div', 'metric-block');
    answersBlock.appendChild(el('h4', 'metric-block-title', 'Raw answers'));
    const ab = el('div', 'answers-block');
    renderAnswersInto(ab, rows);
    answersBlock.appendChild(ab);
    wrap.appendChild(answersBlock);

    return wrap;
  }

  function renderEvalBarsInto(body, results) {
    EVAL_METRICS.forEach(m => {
      const grp = el('div', 'metric-group');
      const head = el('div', 'metric-head');
      head.appendChild(el('span', 'metric-name', m.label));
      head.appendChild(el('span', 'metric-hint', m.hint));
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

  function renderRecommendationsInto(box, results) {
    results.forEach(r => {
      const ev = r.evaluation || {};
      const s = r.summary || {};
      const finalVerdict = s.final_verdict || ev.final_verdict || s.verdict || ev.verdict || '—';
      const metricVerdict = s.deterministic_verdict || ev.deterministic_verdict || '—';
      const judgeVerdict = s.judge_verdict || ev.judge_verdict || '—';
      const item = el('div', `recommendation ${String(finalVerdict).toLowerCase()}`);
      item.appendChild(el('div', 'rec-head', `${r.connection}: ${finalVerdict}`));
      item.appendChild(el('div', 'rec-context', `metric: ${metricVerdict} · judge: ${judgeVerdict} · evaluator: ${ev.rag_model || ev.evaluator || 'deterministic'}`));
      item.appendChild(el('div', 'rec-body', ev.recommendation || 'No QSL recommendation available.'));
      if (ev.metric_issue) {
        item.appendChild(el('div', 'rec-context', `Metric note: ${ev.metric_issue}`));
      }
      if (ev.qsl_context && ev.qsl_context.length) {
        const ctx = el('div', 'rec-context', `QSL context: ${ev.qsl_context.map(x => x.test + '/' + x.connection).join(', ')}`);
        item.appendChild(ctx);
      }
      box.appendChild(item);
    });
  }

  function renderBarsInto(body, results) {
    METRICS.forEach(m => {
      // Some metrics only apply to certain tests by design — e.g.
      // criterion_pass_rate needs a configured `criterion` regex, and
      // assentation_flip_rate needs `test_assentation: true`. If every
      // result in this test group has no value for this metric, it isn't
      // that the models "failed" it — the test simply doesn't measure it.
      // Skip the whole row instead of showing a confusing blank "—" per model.
      const anyValue = results.some(r => r.summary && r.summary[m.key] != null);
      if (!anyValue) return;

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
        row.appendChild(el('span', 'bar-value', val == null ? 'n/a for this test' : `${(val * 100).toFixed(1)}%`));
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
      if (s.verdict || r.evaluation) {
        const finalVerdict = s.final_verdict || s.verdict || r.evaluation?.final_verdict || '—';
        const verdict = el('div', `verdict ${String(finalVerdict || '').toLowerCase()}`, finalVerdict || '—');
        card.appendChild(verdict);
        const metricVerdict = s.deterministic_verdict || r.evaluation?.deterministic_verdict;
        const judgeVerdict = s.judge_verdict || r.evaluation?.judge_verdict;
        if (metricVerdict || judgeVerdict) {
          card.appendChild(el('div', 'rc-model', `metric: ${metricVerdict || '—'} · judge: ${judgeVerdict || '—'}`));
        }
      }
      EVAL_METRICS.forEach(m => {
        if (s[m.key] == null) return;
        const line = el('div', 'metric');
        line.appendChild(el('span', null, m.label));
        line.appendChild(el('span', null, `${(s[m.key] * 100).toFixed(1)}%`));
        card.appendChild(line);
      });
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

  function renderAnswersInto(box, results) {
    results.forEach(r => {
      const d = el('details');
      const s = el('summary');
      s.appendChild(el('span', null, r.connection));
      s.appendChild(el('span', 'dim', `${(r.answers || []).length} answer(s)`));
      d.appendChild(s);
      const body = el('div', 'answer-body');
      if (r.evaluation && r.evaluation.recommendation) {
        const parts = [];
        if (r.evaluation.deterministic_verdict) parts.push(`metric: ${r.evaluation.deterministic_verdict}`);
        if (r.evaluation.judge_verdict) parts.push(`judge: ${r.evaluation.judge_verdict}`);
        if (r.evaluation.final_verdict) parts.push(`final: ${r.evaluation.final_verdict}`);
        const prefix = parts.length ? parts.join(' · ') + ' — ' : '';
        const rec = el('div', 'eval-note', prefix + r.evaluation.recommendation);
        body.appendChild(rec);
      }
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

  // ------- LATEST RESULT ONLY -------
  async function loadLatestResults(opts = {}) {
    const quiet = opts.quiet !== false;
    try {
      const progress = await api('/api/progress').catch(() => null);
      if (progress && ((progress.run && progress.run.active) || (progress.evaluate && progress.evaluate.active))) {
        return false;
      }

      const latest = await api('/api/latest-result');
      if (latest && Array.isArray(latest.results) && latest.results.length) {
        state.lastResults = latest;
        renderResults(state.lastResults, { scroll: opts.scroll === true });
        return true;
      }
      return false;
    } catch (e) {
      if (!quiet) toast(`Could not load latest result: ${e.message}`, 'err');
      return false;
    }
  }

  // If a Run or Evaluate is still active on the server when this page loads
  // (e.g. it was started, then the tab was closed or reloaded), pick up its
  // live percentage. When the operation finishes, Results is refreshed from
  // the newest timestamped output only — older runs are never rendered.
  async function reconnectToActiveOperations() {
    let snap;
    try { snap = await api('/api/progress'); } catch { return false; }
    const runActive = !!(snap.run && snap.run.active);
    const evalActive = !!(snap.evaluate && snap.evaluate.active);

    if (runActive) {
      const btn = $('runBtn'); const label = $('runBtnLabel');
      btn.disabled = true;
      const iv = startProgressPoll('run', label, 'Running');
      $('runNote').textContent = 'Reconnected to a run already in progress…';
      const check = setInterval(async () => {
        const s = await api('/api/progress').catch(() => null);
        if (!s || !s.run.active) {
          clearInterval(check);
          btn.disabled = false;
          stopProgressPoll(iv, label, 'Run tests');
          $('runNote').textContent = '';
          loadLatestResults({ quiet: true, scroll: true });
        }
      }, 1200);
    }

    if (evalActive) {
      const btn = $('evaluateOnlyBtn'); const label = $('evalBtnLabel');
      btn.disabled = true;
      const iv = startProgressPoll('evaluate', label, 'Evaluate');
      const check = setInterval(async () => {
        const s = await api('/api/progress').catch(() => null);
        if (!s || !s.evaluate.active) {
          clearInterval(check);
          btn.disabled = false;
          stopProgressPoll(iv, label, 'Evaluate');
          loadLatestResults({ quiet: true, scroll: true });
        }
      }, 1200);
    }

    return runActive || evalActive;
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
      $('evaluateOnlyBtn').addEventListener('click', onEvaluateOnly);
      const active = await reconnectToActiveOperations();
      if (!active) loadLatestResults({ quiet: true, scroll: false });
    } catch (e) {
      status.textContent = 'API error';
      status.classList.add('err');
      toast(`Failed to load config: ${e.message}`, 'err');
    }
  });
})();
