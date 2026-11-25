(function () {
    'use strict';

    var API_BASE = '/api/reports/stream';
    var AGENTS = {
        'researcher_node': { name: 'Researcher', step: 1, role: 'Plans key questions and gathers sources' },
        'writer_node': { name: 'Writer', step: 2, role: 'Writes and revises the report' },
        'reviewer_node': { name: 'Reviewer', step: 3, role: 'Scores the draft and returns feedback' },
        'final_node': { name: 'Final Editor', step: 4, role: 'Combines all information into the final report' }
    };

    // ---------- find elements ----------
    function byId(ids) {
        for (var i = 0; i < ids.length; i++) {
            var el = document.getElementById(ids[i]);
            if (el) return el;
        }
        return null;
    }

    function findLogBox() {
        var el = byId(['log', 'activityLog', 'liveActivity', 'agentActivity', 'output', 'logs']);
        if (el) return el;
        return document.getElementById('log');
    }

    function findButton() {
        var el = byId(['generateBtn', 'startBtn', 'generate', 'generateReport', 'submitBtn']);
        if (el) return el;
        return document.getElementById('generateBtn');
    }

    var els = {
        topic: byId(['topic']),
        tone: byId(['tone']),
        length: byId(['length']),
        button: findButton(),
        log: findLogBox(),
        progressBar: byId(['progressBar']),
        report: byId(['report-content'])
    };

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
        line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
        if (kind === 'error') line.style.color = '#c0362c';
        if (kind === 'success') line.style.color = '#1a7f4b';
        if (kind === 'warn') line.style.color = '#b7791f';
        els.log.appendChild(line);
        els.log.scrollTop = els.log.scrollHeight;
    }

    function setProgressBar(percent) {
        percent = Math.max(0, Math.min(100, Math.round(percent)));
        if (percent < maxPercent && percent !== 0) percent = maxPercent;
        maxPercent = percent;
        var bar = els.progressBar;
        if (!bar) return;
        if (bar.tagName === 'PROGRESS') {
            bar.max = 100;
            bar.value = percent;
            return;
        }
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

    // ---------- map agent names to UI elements ----------
    function agentToDot(node) {
        var dot = document.createElement('div');
        dot.className = 'step-dot';
        dot.textContent = AGENTS[node].name[0];
        return dot;
    }

    function agentToStep(node, state) {
        var step = document.createElement('li');
        step.className = `step ${state}`;
        step.innerHTML = `
            ${agentToDot(node)} ${AGENTS[node].name} ${AGENTS[node].role}
        `;
        return step;
    }

    // ---------- event handlers ----------
    function onStart(data) {
        log(`Starting report on: ${data.topic || ''}`);
        setProgressBar(25);
    }

    function onProgress(data) {
        if (data.update.error) {
            log(`Step failed: ${data.update.error}`, 'error');
            return;
        }

        var name = data.node || 'Agent';
        log(data.message || `${name} finished`);

        var step = Number(data.step);
        var total = Number(data.total_steps) || 4;
        if (step) {
            setProgressBar((step / total) * 100);
        }

        if (data.node === 'reviewer_node' && data.update.feedback && data.update.feedback.overall_score) {
            var review = String(data.update.feedback.overall_score).split('\n').filter(Boolean).join(' | ');
            log(`Review: ${review}`, /verdict:\s*pass/i.test(review) ? 'success' : 'warn');
        }
        if (data.node === 'writer_node' && data.round) {
            log(`Draft ${data.round} written`);
        }

        // Update agent steps UI
        var steps = document.getElementById('steps');
        if (!steps) return;

        for (var node in AGENTS) {
            var state = AGENTS[node].step <= step ? 'done' : 'running';
            var stepElement = document.querySelector(`#steps li[data-node="${node}"]`);
            if (stepElement) {
                stepElement.className = `step ${state}`;
            } else {
                var newStep = agentToStep(node, state);
                steps.appendChild(newStep);
                newStep.setAttribute('data-node', node);
            }
        }
    }

    function onDone(data) {
        finished = true;
        closeSource();
        setRunning(false);

        var report = data.report || data.state.final_report || data.state.draft || '';
        var wordCount = report.trim().split(/\s+/).length;

        if (!report) {
            log('The workflow finished but returned no report.', 'error');
            return;
        }

        setProgressBar(100);
        els.report.innerHTML = renderMarkdown(report);
        log(`Report ready (${wordCount} words)`, 'success');
        els.report.scrollIntoView({ behavior: 'smooth', block: 'start' });

        // Update word count
        document.getElementById('wordCount').textContent = `Word count: ${wordCount}`;
    }

    function onServerError(data) {
        finished = true;
        closeSource();
        setRunning(false);
        log(`Error: ${data.message || 'The workflow failed.'}`, 'error');
    }

    // ---------- render markdown ----------
    function inline(text) {
        var s = escapeHtml(text);
        s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/(^|[^*])\*([^*\s][^*])*\*/g, '$1<em>$2</em>');
        s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"']+)\)/g,
            '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
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

    // ---------- start ----------
    function startReport() {
        var topic = (els.topic && els.topic.value) || '';
        if (topic.length < 3) { log('Please enter a topic of at least 3 characters.', 'error'); return; }

        closeSource();
        finished = false;
        maxPercent = 0;
        if (els.log) els.log.innerHTML = '';
        if (els.report) els.report.innerHTML = '';
        setProgressBar(0);
        setRunning(true);
        log('Connecting...');

        var length = els.length && els.length.value || 'detailed';
        var tone = els.tone && els.tone.value || 'professional';

        var params = new URLSearchParams({
            topic: topic,
            tone: tone,
            report_length: length
        });
        var url = API_BASE + '?' + params.toString();

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

        source.addEventListener('error', function (e) {
            if (e && e.data) {
                try { onServerError(JSON.parse(e.data)); } catch (err) { onServerError({ message: e.data }); }
                return;
            }
            if (finished) return;
            finished = true;
            closeSource();
            setRunning(false);
            log('Connection to the server was lost. Check the terminal running app.py for the error.', 'error');
        });
    }

    // ---------- wire up ----------
    var form = els.button && els.button.closest('form') || null;
    if (form) {
        form.addEventListener('submit', function (e) { e.preventDefault(); startReport(); });
    }
    if (els.button) {
        els.button.addEventListener('click', function (e) {
            if (form) return;
            e.preventDefault();
            startReport();
        });
    }
    window.addEventListener('beforeunload', closeSource);
})();