let activeSource = null;
let latestMarkdown = "";
let maxPercent = 0;

function appendLog(message, cssClass = "") {
    const logs = document.getElementById("logs");
    if (!logs) return;
    const entry = document.createElement("div");
    entry.className = `log-entry ${cssClass}`.trim();
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${message}`;
    logs.appendChild(entry);
    logs.scrollTop = logs.scrollHeight;
}

function setStatus(text, visible = true) {
    const status = document.getElementById("status");
    if (!status) return;
    status.textContent = text;
    status.style.display = visible ? "block" : "none";
}

function setProgress(percent) {
    const fill = document.getElementById("progress-fill");
    if (!fill) return;
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function escapeHtml(text) {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function renderBasicMarkdown(md) {
    const safe = escapeHtml(md);
    return safe
        .replace(/^### (.+)$/gm, "<h3>$1</h3>")
        .replace(/^## (.+)$/gm, "<h2>$1</h2>")
        .replace(/^# (.+)$/gm, "<h1>$1</h1>")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(
            /(https?:\/\/[^\s<]+)/g,
            '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
        )
        .replace(/^- (.+)$/gm, "<li>$1</li>")
        .replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`)
        .replace(/\n\n+/g, "<br><br>");
}

function startReport() {
    const topicEl = document.getElementById("topic");
    const toneEl = document.getElementById("tone");
    const lengthEl = document.getElementById("length");

    const topic = (topicEl ? topicEl.value : "").trim();
    const tone = toneEl ? toneEl.value : "professional";
    const reportLength = lengthEl ? lengthEl.value : "detailed";

    if (topic.length < 3) {
        setStatus("Please enter a topic (at least 3 characters).");
        appendLog("Please enter a topic of at least 3 characters.", "log-error");
        return;
    }

    if (activeSource) {
        activeSource.close();
        activeSource = null;
    }

    const generateBtn = document.getElementById("generateBtn");
    const copyBtn = document.getElementById("copyBtn");
    const resultDiv = document.getElementById("result");
    const reviewBox = document.getElementById("review-box");
    const logs = document.getElementById("logs");

    if (generateBtn) generateBtn.disabled = true;
    if (copyBtn) copyBtn.style.display = "none";
    if (resultDiv) {
        resultDiv.style.display = "none";
        resultDiv.innerHTML = "";
    }
    if (reviewBox) reviewBox.style.display = "none";
    if (logs) logs.innerHTML = "";

    latestMarkdown = "";
    maxPercent = 5;
    setProgress(maxPercent);
    setStatus("Connecting to multi-agent workflow...");

    const params = new URLSearchParams({
        topic,
        report_length: reportLength,
        tone,
    });

    const source = new EventSource(`/api/reports/stream?${params.toString()}`);
    activeSource = source;
    let finished = false;

    source.addEventListener("start", (e) => {
        const data = JSON.parse(e.data);
        appendLog(`Started workflow for "${data.topic}"`);
        setStatus("Researcher is gathering sources...");
    });

    source.addEventListener("progress", (e) => {
        const data = JSON.parse(e.data);
        if (data.total_steps && data.step) {
            const pct = Math.min(95, Math.round((data.step / data.total_steps) * 95));
            maxPercent = Math.max(maxPercent, pct);
            setProgress(maxPercent);
        }
        if (data.message) setStatus(data.message);
        appendLog(data.message || data.node, `log-${data.node || ""}`);
    });

    source.addEventListener("done", (e) => {
        finished = true;
        source.close();
        activeSource = null;

        const data = JSON.parse(e.data);
        latestMarkdown = data.report || "";

        setProgress(100);
        setStatus("Report completed.");
        appendLog("Workflow finished successfully.", "log-done");

        if (reviewBox && data.review) {
            reviewBox.textContent = `Reviewer Notes:\n${data.review}`;
            reviewBox.style.display = "block";
        }

        if (resultDiv) {
            resultDiv.innerHTML = renderBasicMarkdown(
                latestMarkdown || "No report content returned."
            );
            resultDiv.style.display = "block";
            resultDiv.scrollIntoView({ behavior: "smooth", block: "start" });
        }

        if (copyBtn) {
            copyBtn.style.display = latestMarkdown ? "inline-block" : "none";
        }
        if (generateBtn) generateBtn.disabled = false;
    });

    source.addEventListener("error", (e) => {
        if (finished) return;

        let msg = "Stream connection lost or server error.";
        if (e.data) {
            try {
                msg = JSON.parse(e.data).message || msg;
            } catch (_) {}
        }

        finished = true;
        source.close();
        activeSource = null;

        setStatus(`Error: ${msg}`);
        appendLog(msg, "log-error");
        if (generateBtn) generateBtn.disabled = false;
    });
}

const generateReport = startReport;

async function copyReport() {
    if (!latestMarkdown) return;
    await navigator.clipboard.writeText(latestMarkdown);
    setStatus("Markdown copied to clipboard.");
}