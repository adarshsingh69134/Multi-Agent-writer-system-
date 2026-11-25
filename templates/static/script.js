/* ============================================================
   script.js - frontend for the Multi-Agent Report Writer
   Four agents: Researcher -> Writer -> Reviewer -> Final Editor

   Talks to app.py:
     GET /api/agents                      the agents in order
     GET /api/reports/stream?topic=...    Server-Sent Events:
         start | progress | done | error

   Element IDs used in frontend.html (all optional except the form,
   the topic input and the steps list):
     topicForm   <form>              topic        <input>
     reportLength <select>           tone         <select>
     startBtn    <button>            stopBtn      <button>
     steps       <ol>/<ul>           progressBar  <div> (width set in %)
     statusText  text                timer        text
     round       text                log          <ul>
     review      <pre>/<div>         report       <div>
     wordCount   text                copyBtn      <button>
     downloadBtn <button>            errorBox     <div>
   ============================================================ */
(function () {
  'use strict';

  const API_BASE = ''; // same origin as app.py. Example: 'http://127.0.0.1:8000'

  // Fallback if /api/agents cannot be reached
  const DEFAULT_AGENTS = [
    { node: 'researcher_node', agent: 'Researcher',   step: 1, role: 'Plans key questions and gathers sources' },
    { node: 'writer_node',     agent: 'Writer',       step: 2, role: 'Writes and revises the report' },
    { node: 'reviewer_node',   agent: 'Reviewer',     step: 3, role: 'Scores the draft and returns feedback' },
    { node: 'final_node',      agent: 'Final Editor', step: 4, role: 'Combines all information into the final report' },
  ];

  const $ = (id) => document.getElementById(id);
  const els = {
    form: $('topicForm'), topic: $('topic'), length: $('reportLength'), tone: $('tone'),
    start: $('startBtn'), stop: $('stopBtn'),
    steps: $('steps'), bar: $('progressBar'), status: $('statusText'), timer: $('timer'),
    round: $('round'), log: $('log'), review: $('review'), report: $('report'),
    words: $('wordCount'), copy: $('copyBtn'), download: $('downloadBtn'), error: $('errorBox'),
  };

  let agents = DEFAULT_AGENTS.slice();
  let source = null;        // EventSource
  let finished = false;     // true after done / error / stop
  let timerId = null;
  let startedAt = 0;
  let lastReport = '';
  let lastTopic = '';

  // ---------- small helpers ----------
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function show(el, visible) { if (el) el.hidden = !visible; }
  function setText(el, text) { if (el) el.textContent = text; }

  function formatTime(ms) {
    const s = Math.floor(ms / 1000);
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  function showError(message) {
    if (els.error) { els.error.textContent = message; show(els.error, true); }
    addLog('Error: ' + message, 'error');
    setText(els.status, 'Something went wrong');
  }

  function addLog(message, kind) {
    if (!els.log) return;
    const li = document.createElement('li');
    li.className = 'log-' + (kind || 'info');
    const time = new Date().toLocaleTimeString();
    li.textContent = '[' + time + '] ' + message;
    els.log.appendChild(li);
    els.log.scrollTop = els.log.scrollHeight;
  }

  // ---------- safe, minimal Markdown renderer (escapes HTML first) ----------
  function inline(text) {
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"']+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/(^|[\s(>])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return s;
  }

  function renderMarkdown(md) {
    const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let para = [];
    let list = null; // 'ul' | 'ol'
    let inCode = false;
    let code = [];

    const flushPara = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } };
    const flushList = () => { if (list) { out.push('</' + list + '>'); list = null; } };

    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) { out.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>'); code = []; inCode = false; }
        else { flushPara(); flushList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }

      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushPara(); flushList();
        const level = m[1].length;
        out.push('<h' + level + '>' + inline(m[2]) + '</h' + level + '>');
      } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
        flushPara();
        if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; }
        out.push('<li>' + inline(m[1]) + '</li>');
      } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
        flushPara();
        if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; }
        out.push('<li>' + inline(m[1]) + '</li>');
      } else if (line.trim() === '') {
        flushPara(); flushList();
      } else {
        flushList();
        para.push(line.trim());
      }
    }
    if (inCode) out.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
    flushPara(); flushList();
    return out.join('\n');
  }

  // ---------- step list ----------
  const STATE_LABEL = { waiting: 'Waiting', running: 'Working...', done: 'Done', error: 'Failed' };

  function renderSteps() {
    if (!els.steps) return;
    els.steps.innerHTML = '';
    agents.forEach((a) => {
      const li = document.createElement('li');
      li.id = 'step-' + a.node;
      li.className = 'step step-waiting';
      li.innerHTML =
        '<span class="step-name">' + escapeHtml(a.step + '. ' + a.agent) + '</span>' +
        '<span class="step-role">' + escapeHtml(a.role || '') + '</span>' +
        '<span class="step-state">' + STATE_LABEL.waiting + '</span>';
      els.steps.appendChild(li);
    });
    updateBar();
  }

  function setStep(node, state) {
    const li = $('step-' + node);
    if (!li) return;
    li.className = 'step step-' + state;
    const label = li.querySelector('.step-state');
    if (label) label.textContent = STATE_LABEL[state] || state;
    updateBar();
  }

  function updateBar() {
    if (!els.bar || !els.steps) return;
    const total = agents.length;
    const done = els.steps.querySelectorAll('.step-done').length;
    const running = els.steps.querySelectorAll('.step-running').length;
    els.bar.style.width = Math.round(((done + running * 0.5) / total) * 100) + '%';
  }

  function nextNodeAfter(node, update) {
    const i = agents.findIndex((a) => a.node === node);
    if (node === 'reviewer_node') {
      const verdict = String((update && update.feedback && update.feedback.overall_score) || '').toLowerCase();
      return verdict.includes('verdict: pass') ? 'final_node' : 'writer_node'; // revise or finish
    }
    return i >= 0 && i < agents.length - 1 ? agents[i + 1].node : null;
  }

  // ---------- run state ----------
  function setRunning(running) {
    if (els.start) els.start.disabled = running;
    if (els.topic) els.topic.disabled = running;
    if (els.length) els.length.disabled = running;
    if (els.tone) els.tone.disabled = running;
    show(els.stop, running);
    if (running) {
      startedAt = Date.now();
      clearInterval(timerId);
      timerId = setInterval(() => setText(els.timer, formatTime(Date.now() - startedAt)), 1000);
    } else {
      clearInterval(timerId);
      if (startedAt) setText(els.timer, formatTime(Date.now() - startedAt));
    }
  }

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  function resetUI() {
    finished = false;
    lastReport = '';
    renderSteps();
    if (els.log) els.log.innerHTML = '';
    if (els.report) els.report.innerHTML = '';
    if (els.review) els.review.textContent = '';
    setText(els.words, '');
    setText(els.round, '');
    setText(els.timer, '00:00');
    show(els.error, false);
    show(els.copy, false);
    show(els.download, false);
  }

  // ---------- event handlers ----------
  function onStart(data) {
    setText(els.status, 'Researcher is working...');
    setStep('researcher_node', 'running');
    addLog('Started report on: ' + (data.topic || lastTopic));
  }

  function onProgress(data) {
    const node = data.node;
    const update = data.update || {};

    if (update.error) {
      setStep(node, 'error');
      addLog(data.message || ('Step failed: ' + update.error), 'error');
      return;
    }

    // A second writer step means the Reviewer asked for a revision
    if (node === 'writer_node' && $('step-reviewer_node') &&
        $('step-reviewer_node').classList.contains('step-done')) {
      setStep('reviewer_node', 'waiting');
    }
    if (node === 'writer_node' && data.round) setText(els.round, 'Draft ' + data.round);

    setStep(node, 'done');
    addLog(data.message || (data.agent + ' finished'));

    if (node === 'reviewer_node' && update.feedback && els.review) {
      els.review.textContent = update.feedback.overall_score || '';
    }

    if (node === 'final_node') {
      agents.forEach((a) => setStep(a.node, 'done'));
      setText(els.status, 'Final Editor finished');
      return;
    }

    const next = nextNodeAfter(node, update);
    if (next) {
      setStep(next, 'running');
      const info = agents.find((a) => a.node === next);
      const revising = node === 'reviewer_node' && next === 'writer_node';
      setText(els.status, (info ? info.agent : next) + (revising ? ' is revising the draft...' : ' is working...'));
      if (revising) addLog('Reviewer asked for changes - sending the draft back to the Writer', 'warn');
    }
  }

  function onDone(data) {
    finished = true;
    closeSource();
    setRunning(false);
    agents.forEach((a) => setStep(a.node, 'done'));
    if (els.bar) els.bar.style.width = '100%';

    lastReport = data.report || (data.state && (data.state.final_report || data.state.draft)) || '';
    if (data.review && els.review) els.review.textContent = data.review;

    if (!lastReport) {
      showError('The workflow finished but returned no report.');
      return;
    }
    if (els.report) els.report.innerHTML = renderMarkdown(lastReport);
    const words = lastReport.trim().split(/\s+/).length;
    setText(els.words, words + ' words');
    setText(els.status, 'Report ready');
    addLog('Report ready (' + words + ' words)', 'success');
    show(els.copy, true);
    show(els.download, true);
  }

  function onServerError(data) {
    finished = true;
    closeSource();
    setRunning(false);
    showError((data && data.message) || 'The workflow failed.');
  }

  // ---------- start / stop ----------
  function startReport(topic) {
    closeSource();
    resetUI();
    lastTopic = topic;
    setRunning(true);
    setText(els.status, 'Connecting...');

    const params = new URLSearchParams({
      topic: topic,
      report_length: els.length ? els.length.value : 'detailed',
      tone: els.tone ? els.tone.value : 'professional',
    });
    source = new EventSource(API_BASE + '/api/reports/stream?' + params.toString());

    const parse = (fn) => (e) => {
      try { fn(JSON.parse(e.data)); } catch (err) { console.error('Bad event data', err, e.data); }
    };
    source.addEventListener('start', parse(onStart));
    source.addEventListener('progress', parse(onProgress));
    source.addEventListener('done', parse(onDone));

    // "error" is both our server event (has data) and the browser's connection error (no data)
    source.addEventListener('error', (e) => {
      if (e && e.data) {
        try { onServerError(JSON.parse(e.data)); } catch (_) { onServerError({ message: e.data }); }
        return;
      }
      if (finished) return;
      // Close so the browser does not auto-reconnect and restart the workflow
      finished = true;
      closeSource();
      setRunning(false);
      showError('Connection to the server was lost. Check that app.py is running.');
    });
  }

  function stopReport() {
    if (finished) return;
    finished = true;
    closeSource();
    setRunning(false);
    setText(els.status, 'Stopped');
    addLog('Stopped by user (the server may finish the current step)', 'warn');
  }

  // ---------- copy / download ----------
  async function copyReport() {
    if (!lastReport) return;
    try {
      await navigator.clipboard.writeText(lastReport);
      addLog('Report copied to clipboard', 'success');
    } catch (_) {
      addLog('Copy failed - select the text and copy manually', 'warn');
    }
  }

  function downloadReport() {
    if (!lastReport) return;
    const name = (lastTopic || 'report').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'report';
    const blob = new Blob([lastReport], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // ---------- init ----------
  async function loadAgents() {
    try {
      const res = await fetch(API_BASE + '/api/agents');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (Array.isArray(data.agents) && data.agents.length) agents = data.agents;
    } catch (err) {
      console.warn('Using default agent list:', err);
    }
    renderSteps();
  }

  function init() {
    loadAgents();
    show(els.stop, false);
    show(els.error, false);
    show(els.copy, false);
    show(els.download, false);

    if (els.form) {
      els.form.addEventListener('submit', (e) => {
        e.preventDefault();
        const topic = (els.topic ? els.topic.value : '').trim();
        if (topic.length < 3) { showError('Please enter a topic of at least 3 characters.'); show(els.error, true); return; }
        if (topic.length > 300) { showError('Topic is too long (max 300 characters).'); return; }
        startReport(topic);
      });
    }
    if (els.stop) els.stop.addEventListener('click', stopReport);
    if (els.copy) els.copy.addEventListener('click', copyReport);
    if (els.download) els.download.addEventListener('click', downloadReport);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();