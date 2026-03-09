const $ = (id) => document.getElementById(id);

let pendingMessage   = null;
let currentSessionId = null;
let currentSort      = "status";
let focusedJob       = null; // { id, company, job_title, status } or null
const recentMessages = []; // last N {role, text} for pronoun resolution

const STATUS_ORDER = { in_process: 0, offer: 1, applied: 2, rejected: 3, abandoned: 4 };

function sortJobs(jobs) {
  const copy = [...jobs];
  if (currentSort === "status") {
    copy.sort((a, b) => {
      const sa = STATUS_ORDER[a.status] ?? 2;
      const sb = STATUS_ORDER[b.status] ?? 2;
      if (sa !== sb) return sa - sb;
      return (a.updated_at || "").localeCompare(b.updated_at || "");
    });
  } else if (currentSort === "date") {
    copy.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  } else if (currentSort === "az") {
    copy.sort((a, b) => (a.company || "").toLowerCase().localeCompare((b.company || "").toLowerCase()));
  }
  return copy;
}

// ── Row / bubble helpers ──────────────────────────────────────────────────────

function addRow(kind, text = "") {
  const row = document.createElement("div");
  row.className = `msg-row ${kind}`;

  // Avatar (all non-user rows)
  if (kind !== "user") {
    const av = document.createElement("div");
    av.className = "avatar ai";
    av.textContent = "✦";
    row.appendChild(av);
  }

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (text) bubble.textContent = text;
  row.appendChild(bubble);

  // User avatar on the right
  if (kind === "user") {
    const av = document.createElement("div");
    av.className = "avatar user";
    av.textContent = "J";
    row.appendChild(av);
  }

  $("messages").appendChild(row);
  scrollBottom();
  return { row, bubble };
}

function scrollBottom() {
  const el = $("messages");
  el.scrollTop = el.scrollHeight;
}

function setSpinner(bubble, text) {
  bubble.textContent = "";
  const s = document.createElement("span");
  s.className = "spinner";
  bubble.appendChild(s);
  bubble.appendChild(document.createTextNode(" " + text));
}

// ── Greeting ──────────────────────────────────────────────────────────────────

function showGreeting() {
  const { bubble } = addRow("ai");
  const b = document.createElement("b");
  b.textContent = "Hi! I'm your job hunt assistant.";
  bubble.appendChild(b);
  bubble.appendChild(document.createTextNode(
    " What can I help you with?\n\n" +
    "• Paste a job URL → starts a new application pipeline\n" +
    "• Log an update  → e.g. \"Thales rejected me\"\n" +
    "• Ask a question → e.g. \"Status of my Databricks app?\""
  ));
}

// ── Job focus ─────────────────────────────────────────────────────────────────

function renderFocusBanner(job) {
  const existing = document.querySelector(".focus-banner");
  if (existing) existing.remove();

  const banner = document.createElement("div");
  banner.className = "focus-banner";

  const label = document.createElement("div");
  label.className = "focus-banner-label";
  label.textContent = job.company + (job.job_title ? ` — ${job.job_title}` : "");
  banner.appendChild(label);

  // Action buttons
  const actions = document.createElement("div");
  actions.className = "focus-banner-actions";

  const rerunBtn = document.createElement("button");
  rerunBtn.className = "focus-btn rerun-btn";
  rerunBtn.textContent = "Re-run ATS";
  rerunBtn.onclick = () => rerunAts(job.id);
  actions.appendChild(rerunBtn);

  const abandonBtn = document.createElement("button");
  abandonBtn.className = "focus-btn abandon-btn";
  abandonBtn.textContent = "Abandon";
  abandonBtn.onclick = () => abandonJob(job.id);
  actions.appendChild(abandonBtn);

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "focus-btn delete-btn";
  deleteBtn.textContent = "Delete";
  deleteBtn.onclick = () => deleteJob(job.id);
  actions.appendChild(deleteBtn);

  banner.appendChild(actions);

  const dismiss = document.createElement("button");
  dismiss.className = "focus-banner-dismiss";
  dismiss.textContent = "×";
  dismiss.onclick = clearFocus;
  banner.appendChild(dismiss);

  const chatWrap = document.querySelector(".chat-wrap");
  chatWrap.insertBefore(banner, chatWrap.firstChild);
}

async function rerunAts(jobId) {
  const btn = document.querySelector(".rerun-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Running..."; }

  const { bubble } = addRow("ai");
  setSpinner(bubble, "Re-running ATS evaluation...");

  try {
    const res = await fetch(`/api/jobs/${jobId}/rerun-ats`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Failed" }));
      bubble.textContent = `ATS re-run failed: ${err.detail || res.status}`;
      return;
    }
    const data = await res.json();
    bubble.textContent = data.comparison_text || "ATS re-evaluation complete.";
    recentMessages.push({ role: "assistant", text: data.comparison_text || "" });
    if (recentMessages.length > 10) recentMessages.splice(0, recentMessages.length - 10);
  } catch (e) {
    bubble.textContent = `Error: ${e.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Re-run ATS"; }
    scrollBottom();
  }
}

async function abandonJob(jobId) {
  if (!confirm("Abandon this application? This marks it as not worth pursuing.")) return;

  try {
    const res = await fetch(`/api/jobs/${jobId}/abandon`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Failed" }));
      addRow("ai", `Failed to abandon: ${err.detail || res.status}`);
      return;
    }
    addRow("ai", "Application abandoned. Moving on.");
    if (focusedJob) focusedJob.status = "abandoned";
    await loadJobs($("jobSearch").value || "");
  } catch (e) {
    addRow("ai", `Error: ${e.message}`);
  }
}

async function deleteJob(jobId) {
  if (!confirm("Delete this job and all its files? This cannot be undone.")) return;

  try {
    const res = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Failed" }));
      addRow("ai", `Failed to delete: ${err.detail || res.status}`);
      return;
    }
    clearFocus();
    await loadJobs($("jobSearch").value || "");
  } catch (e) {
    addRow("ai", `Error: ${e.message}`);
  }
}

async function loadConversation(jobId) {
  try {
    const res = await fetch(`/api/jobs/${jobId}/conversation`);
    if (!res.ok) {
      addRow("ai", `Could not load conversation (${res.status}).`);
      return;
    }
    const data = await res.json();
    if (!data.messages || data.messages.length === 0) {
      const name = data.company || "this job";
      addRow("ai", `Focused on ${name}. Ask me anything — I have the full JD, ATS report, and resume on hand.`);
      return;
    }
    // Replay history
    const toSeed = [];
    for (const m of data.messages) {
      addRow(m.role === "user" ? "user" : "ai", m.text);
      toSeed.push({ role: m.role, text: m.text });
    }
    // Seed recentMessages with last 10
    const seed = toSeed.slice(-10);
    recentMessages.splice(0, recentMessages.length, ...seed);
  } catch (err) {
    console.error("loadConversation failed:", err);
    addRow("ai", "Failed to load conversation history.");
  }
}

async function focusJob(job) {
  if (currentSessionId) return; // don't interrupt an active pipeline

  try {
    focusedJob = job;

    // Update active card highlight
    document.querySelectorAll(".job-card").forEach(c => {
      c.classList.toggle("active", c.dataset.jobId === String(job.id));
    });

    renderFocusBanner(job);

    // Clear chat
    $("messages").innerHTML = "";
    recentMessages.splice(0, recentMessages.length);

    await loadConversation(job.id);
  } catch (err) {
    console.error("focusJob failed:", err);
    addRow("ai", "Something went wrong loading this job.");
  }
}

function clearFocus() {
  focusedJob = null;
  recentMessages.splice(0, recentMessages.length);

  const banner = document.querySelector(".focus-banner");
  if (banner) banner.remove();

  document.querySelectorAll(".job-card").forEach(c => c.classList.remove("active"));

  $("messages").innerHTML = "";
  showGreeting();
}

// ── Sidebar jobs ──────────────────────────────────────────────────────────────

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en", { month: "short", day: "numeric" });
}

async function loadJobs(q = "") {
  const url = q ? `/api/jobs?q=${encodeURIComponent(q)}` : "/api/jobs";
  const data = await fetch(url).then(r => r.json());
  const root = $("jobs");
  root.innerHTML = "";

  if (!data.jobs.length) {
    const el = document.createElement("div");
    el.className = "job-empty";
    el.textContent = q ? "No results" : "No jobs yet";
    root.appendChild(el);
    return;
  }

  for (const j of sortJobs(data.jobs)) {
    // Fallback display name derived from folder when company is blank
    const folderName = (j.artifact_dir || "").split("/").pop() || "";
    const folderCompany = folderName
      ? folderName.split("__")[0].replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase())
      : "";
    const displayName = j.company || folderCompany || "(unknown)";

    const card = document.createElement("div");
    card.className = "job-card";
    if (j.status === "rejected") card.classList.add("rejected");
    if (j.status === "abandoned") card.classList.add("abandoned");
    const isUnknown = !j.company && !j.job_title;
    if (isUnknown) card.classList.add("unknown-data");

    const title = document.createElement("div");
    title.className = "job-card-title";
    title.textContent = displayName;
    card.appendChild(title);

    if (j.job_title) {
      const sub = document.createElement("div");
      sub.className = "job-card-title-sub";
      sub.textContent = j.job_title.length > 40 ? j.job_title.slice(0, 40) + "…" : j.job_title;
      card.appendChild(sub);
    }

    const meta = document.createElement("div");
    meta.className = "job-card-meta";
    const dot = document.createElement("span");
    dot.className = `status-dot ${j.status || ""}`;
    meta.appendChild(dot);
    meta.appendChild(document.createTextNode(j.status || "draft"));

    if (j.updated_at) {
      const dateEl = document.createElement("span");
      dateEl.className = "job-date";
      dateEl.textContent = fmtDate(j.updated_at);
      meta.appendChild(dateEl);
    }

    card.appendChild(meta);

    card.dataset.jobId = String(j.id);
    card.onclick = () => focusJob({ id: j.id, company: displayName, job_title: j.job_title, status: j.status });

    root.appendChild(card);
  }

  // Re-apply active highlight after re-render
  if (focusedJob) {
    document.querySelectorAll(".job-card").forEach(c => {
      if (c.dataset.jobId === String(focusedJob.id)) c.classList.add("active");
    });
  }
}

// ── Input helpers ─────────────────────────────────────────────────────────────

function autoResize() {
  const ta = $("input");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

function setHint(text) { $("hint").textContent = text; }

function checkUrlBadge() {
  const hasUrl = /https?:\/\/\S+/.test($("input").value);
  $("url-badge").classList.toggle("hidden", !hasUrl);
}

// ── sendMessage ───────────────────────────────────────────────────────────────

async function sendMessage(message, jobId = null) {
  pendingMessage = message;

  // Track for context window
  recentMessages.push({ role: "user", text: message });
  if (recentMessages.length > 10) recentMessages.splice(0, recentMessages.length - 10);

  addRow("user", message);

  // Working bubble — we'll update this throughout the response
  const { row, bubble } = addRow("ai");
  setSpinner(bubble, "Thinking…");

  // Pipeline sub-elements created lazily on first pipeline event
  let stepLine   = null;  // <div class="step-line"> inside bubble
  let fieldsList = null;  // <div> container appended after stepLine
  let coachingBubble = null; // spinner bubble shown while coaching generates

  function ensurePipeline() {
    if (stepLine) return;
    row.className = "msg-row pipeline";
    bubble.textContent = "";

    stepLine = document.createElement("div");
    stepLine.className = "step-line";
    bubble.appendChild(stepLine);

    fieldsList = document.createElement("div");
    bubble.appendChild(fieldsList);
  }

  function setStep(text) {
    ensurePipeline();
    stepLine.textContent = "";
    const s = document.createElement("span");
    s.className = "spinner";
    stepLine.appendChild(s);
    stepLine.appendChild(document.createTextNode(" " + text));
    scrollBottom();
  }

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, job_id: jobId, session_id: currentSessionId, context: recentMessages.slice(-8), focused_job_id: focusedJob ? focusedJob.id : null }),
  });

  if (!res.ok || !res.body) {
    row.className = "msg-row error";
    bubble.textContent = `Request failed: ${res.status}`;
    return;
  }

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;

      let evt;
      try { evt = JSON.parse(line); } catch { continue; }

      if (evt.session_id) currentSessionId = evt.session_id;

      // ── status (regular chat thinking) ───────────────────────────────────
      if (evt.type === "status") {
        row.className = "msg-row ai";
        setSpinner(bubble, evt.text);

      // ── pipeline_step ─────────────────────────────────────────────────────
      } else if (evt.type === "pipeline_step") {
        setStep(evt.text);

      // ── pipeline_field ────────────────────────────────────────────────────
      } else if (evt.type === "pipeline_field") {
        ensurePipeline();
        // Update step line to remove spinner once a field arrives
        const line = document.createElement("div");
        line.className = "field-line";
        const key = document.createElement("span");
        key.className = "fkey";
        key.textContent = (evt.label || evt.key) + ": ";
        const val = document.createElement("span");
        val.className = "fval";
        val.textContent = "✓ " + evt.value;
        line.appendChild(key);
        line.appendChild(val);
        fieldsList.appendChild(line);
        scrollBottom();

      // ── pipeline_ask ──────────────────────────────────────────────────────
      } else if (evt.type === "pipeline_ask") {
        ensurePipeline();
        stepLine.textContent = evt.text;
        $("input").value = evt.suggested || "";
        autoResize();
        setHint(`Confirm or correct the ${evt.label} and press Enter`);

      // ── pipeline_resume ───────────────────────────────────────────────────
      } else if (evt.type === "pipeline_resume") {
        ensurePipeline();
        const card = document.createElement("div");
        card.className = "resume-line";
        const t = document.createElement("div");
        t.className = "resume-line-title";
        t.textContent = "📄 " + evt.filename;
        card.appendChild(t);
        if (evt.reasoning) {
          const r = document.createElement("div");
          r.className = "resume-line-reason";
          r.textContent = evt.reasoning;
          card.appendChild(r);
        }
        fieldsList.appendChild(card);
        scrollBottom();

      // ── pipeline_complete ─────────────────────────────────────────────────
      } else if (evt.type === "pipeline_complete") {
        ensurePipeline();
        // Clear spinner from step line
        if (stepLine) stepLine.textContent = "";

        const card = document.createElement("div");
        card.className = "complete-card";

        const title = document.createElement("div");
        title.className = "complete-card-title";
        title.textContent = "✓ Job created";
        card.appendChild(title);

        const rows = [
          evt.company && evt.job_title ? `${evt.company} — ${evt.job_title}` : null,
          evt.selected_resume ? `Resume: ${evt.selected_resume}` : null,
          evt.ats_result      ? `ATS eval: ${evt.ats_result}` : null,
        ].filter(Boolean);
        for (const r of rows) {
          const el = document.createElement("div");
          el.className = "complete-card-row";
          el.textContent = r;
          card.appendChild(el);
        }
        fieldsList.appendChild(card);
        scrollBottom();

        setHint("Enter ↵ to send · Shift+Enter for newline");
        await loadJobs($("jobSearch").value || "");

        // Auto-focus the newly created job
        if (evt.job_id) {
          const newJob = {
            id: evt.job_id,
            company: evt.company || "",
            job_title: evt.job_title || "",
            status: "applied",
          };
          focusedJob = newJob;
          renderFocusBanner(newJob);
          // Highlight in sidebar
          document.querySelectorAll(".job-card").forEach(c => {
            c.classList.toggle("active", c.dataset.jobId === String(evt.job_id));
          });
        }

        // Show coaching spinner if ATS report exists (coaching will follow)
        if (evt.ats_report) {
          const { bubble: cb } = addRow("ai");
          setSpinner(cb, "Generating coaching assessment...");
          coachingBubble = cb;
        }

      // ── pipeline_coaching ───────────────────────────────────────────────
      } else if (evt.type === "pipeline_coaching") {
        // Replace coaching spinner with actual text, or create new bubble
        if (coachingBubble) {
          coachingBubble.textContent = evt.text || "";
        } else {
          const { bubble: cb } = addRow("ai");
          cb.textContent = evt.text || "";
          coachingBubble = cb;
        }
        recentMessages.push({ role: "assistant", text: evt.text || "" });
        if (recentMessages.length > 10) recentMessages.splice(0, recentMessages.length - 10);

      // ── pipeline_error ────────────────────────────────────────────────────
      } else if (evt.type === "pipeline_error") {
        row.className = "msg-row error";
        bubble.textContent = evt.text || "Pipeline error.";
        currentSessionId = null;
        setHint("Enter ↵ to send · Shift+Enter for newline");

      // ── clarify ───────────────────────────────────────────────────────────
      } else if (evt.type === "clarify") {
        row.className = "msg-row ai";
        bubble.textContent = evt.text;
        const wrap = document.createElement("div");
        wrap.className = "options";
        for (const opt of evt.options || []) {
          const b = document.createElement("button");
          b.className = "opt";
          b.textContent = opt.label;
          b.onclick = () => pendingMessage && sendMessage(pendingMessage, opt.id);
          wrap.appendChild(b);
        }
        bubble.appendChild(wrap);
        scrollBottom();

      // ── answer ────────────────────────────────────────────────────────────
      } else if (evt.type === "answer") {
        row.className = "msg-row ai";
        bubble.textContent = evt.text || "";
        recentMessages.push({ role: "assistant", text: evt.text || "" });
        if (recentMessages.length > 10) recentMessages.splice(0, recentMessages.length - 10);

      // ── ok ────────────────────────────────────────────────────────────────
      } else if (evt.type === "ok") {
        row.className = "msg-row ai";
        const e = evt.event || {};

        // Natural LLM response as the main bubble text
        bubble.textContent = evt.text || "Got it, logged.";

        // Hardcoded technical confirmation below
        const meta = document.createElement("div");
        meta.className = "msg-meta";
        meta.textContent = evt.meta || `event logged · ${e.event_type} · ${e.event_date}`;
        bubble.appendChild(meta);

        recentMessages.push({ role: "assistant", text: evt.text || "" });
        if (recentMessages.length > 10) recentMessages.splice(0, recentMessages.length - 10);

        await loadJobs($("jobSearch").value || "");

      // ── error ─────────────────────────────────────────────────────────────
      } else if (evt.type === "error") {
        row.className = "msg-row error";
        bubble.textContent = evt.text || "Error";
      }
    }
  }

  // Defensive cleanup — if stream ended without explicit complete/error
  if (currentSessionId) {
    currentSessionId = null;
  }

  // Remove orphaned coaching spinner if coaching never arrived
  if (coachingBubble && coachingBubble.querySelector(".spinner")) {
    coachingBubble.closest(".msg-row")?.remove();
  }
}

// ── Bind UI ───────────────────────────────────────────────────────────────────

function bind() {
  $("send").onclick = () => {
    const msg = $("input").value.trim();
    if (!msg) return;
    $("input").value = "";
    autoResize();
    $("url-badge").classList.add("hidden");
    sendMessage(msg);
  };

  $("input").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    $("send").click();
  });

  $("input").addEventListener("input", () => {
    autoResize();
    checkUrlBadge();
  });

  $("jobSearch").addEventListener("input", (e) => loadJobs(e.target.value || ""));

  document.querySelectorAll(".sort-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      currentSort = btn.dataset.sort;
      document.querySelectorAll(".sort-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      loadJobs($("jobSearch").value || "");
    });
  });
}

bind();
showGreeting();
loadJobs();
