/* script.js - Multi-Agent Report Writer frontend
 *
 * Works with the backend stream:
 *   GET /api/reports/stream?topic=...&tone=...&length=...
 * Events: start | progress | done | error
 *
 * It finds your page elements by common IDs first, then by position
 * (first text input, first two <select>s, the Generate button, the box under
 * "Live Agent Activity"), so it should work with the page in your screenshot.
 */
(function () {
  'use strict';

  var API_BASE = ''; // same origin as app.py

  // ---------- find elements ----------
  function byId(ids) {
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (el) return el;
    }
    return null;
  }

  function findLogBox() {
    var el = byId(['log', 'activityLog', 'liveActivity', 'agentActivity', 'activity', 'output', 'logs']);
    if (el) return el;
    var heads = document.querySelectorAll('h1,h2,h3,h4,label,strong');
    for (var i = 0; i < heads.length; i++) {
      if (/live agent activity/i.test(heads[i].textContent)) {
        var next = heads[i].nextElementSibling;
        if (next) return next;
      }
    }
    return null;
  }

  function findButton() {
    var el = byId(['generateBtn', 'startBtn', 'generate', 'generateReport', 'submitBtn']);
    if (el) return el;
    var btns = document.querySelectorAll('button, input[type=submit]');
    for (var i = 0; i < btns.length; i++) {
      if (/generate/i.test(btns[i].textContent || btns[i].value || '')) return btns[i];
    }
    return btns[0] || null;
  }

  var selects = document.querySelectorAll('select');
  var els = {
    topic: byId(['topic', 'reportTopic', 'topicInput']) || document.querySelector('input[type=text], input:not([type])'),
    tone: byId(['tone', 'toneSelect']) || selects[0] || null,
    length: byId(['length', 'reportLength', 'lengthSelect']) || selects[1] || null,
    button: findButton(),
    log: findLogBox(),
    bar: byId(['progressBar', 'progress', 'bar']) || document.querySelector('progress, .progress-bar, .progress'),
    report: byId(['report', 'reportOutput', 'reportContent', 'result', 'finalReport'])
  };

  // create a report area if the page has none
  if (!els.report) {
    els.report = document.createElement('div');
    els.report.id = 'report';
    els.report.style.cssText = 'margin-top:20px;line-height:1.6;';
    var anchor = els.log && els.log.parentNode ? els.log : document.body;
    if (anchor.parentNode && anchor !== document.body) {
      anchor.parentNode.insertBefore(els.report, anchor.nextSibling);
    } else {
      document.body.appendChild(els.report);
    }
  }

  // ---------- state ----------
  var source = null;
  var finished = false;
  var maxPercent = 0;

  // ---------- helpers ----------
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function log(message, kind) {
    if (!els.log) { console.log(message); return; }
    var line = document.createElement('div');
    line.textContent = '[' + new Date().toLocaleTimeString() + '] ' + message;
    if (kind === 'error') line.style.color = '#c0362c';
    if (kind === 'success') line.style.color = '#1a7f4b';
    if (kind === 'warn') line.style.color = '#b7791f';
    els.log.appendChild(line);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setBar(percent) {
    percent = Math.max(0, Math.min(100, Math.round(percent)));
    if (percent < maxPercent && percent !== 0) percent = maxPercent; // revisions must not move it back
    maxPercent = percent;
    var bar = els.bar;
    if (!bar) return;
    if (bar.tagName === 'PROGRESS') { bar.max = 100; bar.value = percent; return; }
    var target = bar.firstElementChild || bar;
    target.style.width = percent + '%';
    if (!bar.firstElementChild && !target.style.height) target.style.minHeight = '6px';
  }

  function setRunning(running) {
    if (els.button) els.button.disabled = running;
    if (els.topic) els.topic.disabled = running;
    if (els.tone) els.tone.disabled = running;
    if (els.length) els.length.disabled = running;
  }

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  // ---------- map dropdown values to what the backend understands ----------
  function lengthValue() {
    var raw = String((els.length && els.length.value) || 'detailed').toLowerCase();
    if (raw.indexOf('summ') !== -1 || raw.indexOf('short') !== -1) return 'short';
    if (raw.indexOf('compre') !== -1 || raw.indexOf('long') !== -1) return 'comprehensive';
    if (raw.indexOf('detail') !== -1) return 'detailed';
    return raw;
  }

  function toneValue() {
    return String((els.tone && els.tone.value) || 'professional').toLowerCase();
  }

  // ---------- small, safe Markdown renderer ----------
  function inline(text) {
    var s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"']+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\u3010(https?:\/\/[^\s\u3011]+)\u3011/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer">[source]</a>');
    s = s.replace(/(^|[\s(>])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return s;
  }

  function renderMarkdown(md) {
    var lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
    var out = [], para = [], list = null, table = [];

    function flushPara() { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } }
    function flushList() { if (list) { out.push('</' + list + '>'); list = null; } }
    function flushTable() {
      if (!table.length) return;
      var rows = table.filter(function (r) { return !/^\s*\|?\s*:?-{2,}/.test(r); });
      var html = '<table style="border-collapse:collapse;margin:10px 0;">';
      rows.forEach(function (r, i) {
        var cells = r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
        var tag = i === 0 ? 'th' : 'td';
        html += '<tr>' + cells.map(function (c) {
          return '<' + tag + ' style="border:1px solid #ccc;padding:6px 10px;text-align:left;">' + inline(c.trim()) + '</' + tag + '>';
        }).join('') + '</tr>';
      });
      out.push(html + '</table>');
      table = [];
    }

    lines.forEach(function (line) {
      var m;
      if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line); return; }
      flushTable();

      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushPara(); flushList();
        out.push('<h' + m[1].length + '>' + inline(m[2]) + '</h' + m[1].length + '>');
      } else if ((m = line.match(/^\s*[-*\u2022]\s+(.*)$/))) {
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
    });
    flushPara(); flushList(); flushTable();
    return out.join('\n');
  }

  // ---------- event handlers ----------
  function onStart(data) {
    log('Started report on: ' + (data.topic || ''));
    setBar(3);
  }

  function onProgress(data) {
    var update = data.update || {};
    if (update.error) {
      log('Step failed (' + (data.agent || data.node) + '): ' + update.error, 'error');
      return;
    }

    var name = data.agent || data.node || 'Agent';
    log(data.message || (name + ' finished'));

    var step = Number(data.step), total = Number(data.total_steps) || 4;
    if (step) setBar((step / total) * 100);

    if (data.node === 'reviewer_node' && update.feedback && update.feedback.overall_score) {
      var review = String(update.feedback.overall_score).split('\n').filter(Boolean).slice(0, 2).join(' | ');
      log('Review: ' + review, /verdict:\s*pass/i.test(review) ? 'success' : 'warn');
    }
    if (data.node === 'writer_node' && data.round) {
      log('Draft ' + data.round + ' written');
    }
  }

  function onDone(data) {
    finished = true;
    closeSource();
    setRunning(false);

    var state = data.state || {};
    var report = data.report || state.final_report || state.draft || '';

    if (!report) {
      log('The workflow finished but returned no report.', 'error');
      return;
    }
    setBar(100);
    els.report.innerHTML = renderMarkdown(report);
    log('Report ready (' + report.trim().split(/\s+/).length + ' words)', 'success');
    els.report.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function onServerError(data) {
    finished = true;
    closeSource();
    setRunning(false);
    log('Error: ' + ((data && data.message) || 'The workflow failed.'), 'error');
  }

  // ---------- start ----------
  function startReport() {
    var topic = (els.topic ? els.topic.value : '').trim();
    if (topic.length < 3) { log('Please enter a topic of at least 3 characters.', 'error'); return; }

    closeSource();
    finished = false;
    maxPercent = 0;
    if (els.log) els.log.innerHTML = '';
    els.report.innerHTML = '';
    setBar(0);
    setRunning(true);
    log('Connecting...');

    var length = lengthValue();
    var params = new URLSearchParams({
      topic: topic,
      tone: toneValue(),
      length: length,          // name used by the earlier working page
      report_length: length    // name used by newer versions
    });
    var url = API_BASE + '/api/reports/stream?' + params.toString();

    try {
      source = new EventSource(url);
    } catch (e) {
      setRunning(false);
      log('Could not open the stream: ' + e.message, 'error');
      return;
    }

    function parse(fn) {
      return function (e) {
        var data;
        try { data = JSON.parse(e.data); } catch (err) { console.error('Bad event data', e.data); return; }
        fn(data);
      };
    }

    source.addEventListener('start', parse(onStart));
    source.addEventListener('progress', parse(onProgress));
    source.addEventListener('done', parse(onDone));

    // "error" is both our server event (has data) and the browser's connection error (no data)
    source.addEventListener('error', function (e) {
      if (e && e.data) {
        try { onServerError(JSON.parse(e.data)); } catch (err) { onServerError({ message: e.data }); }
        return;
      }
      if (finished) return;
      finished = true;                 // stop the browser from reconnecting and restarting the run
      closeSource();
      setRunning(false);
      log('Connection to the server was lost. Check the terminal running app.py for the error.', 'error');
    });
  }

  // ---------- wire up ----------
  var form = els.button && els.button.closest ? els.button.closest('form') : null;
  if (form) {
    form.addEventListener('submit', function (e) { e.preventDefault(); startReport(); });
  }
  if (els.button) {
    els.button.addEventListener('click', function (e) {
      if (form) return;                // the submit handler above will run
      e.preventDefault();
      startReport();
    });
  }
  window.addEventListener('beforeunload', closeSource);
})();