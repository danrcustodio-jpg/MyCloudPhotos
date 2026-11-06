/* My Cloud Photos — frontend */

const state = {
  view: "browse",
  offset: 0,
  limit: 120,
  total: 0,
  selected: new Set(),
  pollTimer: null,
  importPoll: null,
  archivePoll: null,
  inventoryPoll: null,
  archiveSelected: new Set(),
  archiveLargestAssets: [],
  archiveSizeById: {},
  archiveTypeById: {},
  archiveOffset: 0,
  archiveLimit: 60,
  archiveTotal: 0,
  archiveAuthExpanded: false,
  archiveInventoryPinned: false,
  archivePickTab: "largest",
  busyPoll: null,
  busyJobId: null,
  jobWaitVisible: false,
  smartFolder: "",
  nameBrowse: "",
  nameBrowseList: [],
  personId: null,
  peopleBrowseList: [],
  sort: "date_desc",
  map: null,
  mapMarkers: null,
  mapLoaded: false,
  icloudAuthFlash: null, // { tone: "error"|"ok", message: string }
  icloudActiveAccountId: null,
  activityStack: [],
  activityShowTimer: null,
  activityTokenSeq: 0,
};

async function api(path, opts = {}) {
  const busyOpt = opts.busy;
  const { busy: _busyIgnored, ...fetchOpts } = opts;
  const method = String(fetchOpts.method || "GET").toUpperCase();
  let token = null;
  if (busyOpt === false) {
    token = null;
  } else if (typeof busyOpt === "string") {
    token = beginActivity(busyOpt);
  } else if (/\/(status|busy)(\?|$)/.test(String(path)) || String(path).includes("/jobs/busy")) {
    // Frequent pollers — never toast
    token = null;
  } else {
    const label = busyLabelFor(path, method);
    token = label ? beginActivity(label) : null;
  }
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(fetchOpts.headers || {}) },
      ...fetchOpts,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text };
    }
    if (!res.ok) {
      const msg = data?.detail || data?.message || res.statusText;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  } finally {
    if (token) endActivity(token);
  }
}

function busyLabelFor(path, method = "GET") {
  const p = String(path || "");
  const m = String(method || "GET").toUpperCase();
  if (p.includes("/icloud/auth/2fa/sms")) return "Requesting SMS 2FA…";
  if (p.includes("/icloud/auth/2fa/resend")) return "Requesting a fresh 2FA code…";
  if (p.includes("/icloud/auth/2fa")) return "Submitting 2FA…";
  if (p.includes("/icloud/auth/reset")) return "Resetting iCloud sign-in…";
  if (p.includes("/icloud/logout")) return "Signing out of iCloud…";
  if (p.includes("/icloud/auth")) return "Signing in to iCloud…";
  if (p.includes("/icloud/accounts/select")) return "Switching family account…";
  if (p.includes("/icloud/inventory/refresh")) return "Starting inventory refresh…";
  if (p.includes("/icloud/inventory/cancel")) return "Cancelling inventory…";
  if (p.includes("/icloud/inventory")) return "Checking inventory status…";
  if (p.includes("/icloud/archive/estimate")) return "Estimating archive space…";
  if (p.includes("/icloud/archive/largest")) return "Loading largest iCloud assets…";
  if (p.includes("/icloud/assets/") && p.includes("/media")) return "Loading iCloud media…";
  if (p.includes("/icloud/archive/assets")) return "Loading matching iCloud assets…";
  if (p.includes("/icloud/archive/start")) return "Starting archive to WD…";
  if (p.includes("/icloud/archive/cancel")) return "Cancelling archive…";
  if (p.includes("/icloud/archive/status")) return "Checking archive status…";
  if (p.includes("/icloud/status")) return "Checking iCloud connection…";
  if (p.includes("/icloud/assets/") && p.includes("/thumb")) return "Loading iCloud thumbnail…";
  if (p.includes("/scan/start")) return "Starting library scan…";
  if (p.includes("/scan/status")) return "Checking scan status…";
  if (p.includes("/import/preview")) return "Previewing import…";
  if (p.includes("/import/start")) return "Starting import…";
  if (p.includes("/duplicates/exact/batch")) return m === "POST" ? "Working on exact dedupe…" : "Checking dedupe status…";
  if (p.includes("/duplicates/")) return "Searching duplicates…";
  if (p.includes("/organize/")) return m === "POST" ? "Working on organize…" : "Checking organize status…";
  if (p.includes("/faces/")) return m === "POST" ? "Working on face scan…" : "Checking face scan…";
  if (p.includes("/raw/")) return m === "POST" ? "Working on RAW convert…" : "Checking RAW convert…";
  if (p.includes("/cleanup/")) return m === "POST" ? "Working on cleanup…" : "Checking cleanup…";
  if (p.includes("/dates/backfill")) {
    return m === "POST" ? "Filling missing dates…" : "Previewing date backfill…";
  }
  if (p.includes("/photos/remove")) {
    return "Removing selected photos…";
  }
  if (p.includes("/map/points")) return "Loading map points…";
  if (p.includes("/photos")) return "Loading photos…";
  if (p.includes("/people")) return m === "POST" ? "Updating people…" : "Loading people…";
  if (p.includes("/quarantine")) return m === "POST" ? "Updating quarantine…" : "Loading quarantine…";
  if (p.includes("/folders") || p.includes("/timeline") || p.includes("/smart-folders")) {
    return "Loading library filters…";
  }
  if (p.includes("/stats")) return "Refreshing stats…";
  if (p.includes("/settings")) return m === "POST" ? "Saving settings…" : "Loading settings…";
  if (p.includes("/jobs/busy")) return null; // never toast for frequent busy polls
  return m === "GET" ? "Loading…" : "Working…";
}

/** Show a sticky activity toast if work lasts longer than a few hundred ms. */
function beginActivity(message, { delayMs = 450 } = {}) {
  if (!message) return null;
  const token = ++state.activityTokenSeq;
  state.activityStack.push({ token, message });
  const show = () => {
    if (!state.activityStack.length) return;
    const top = state.activityStack[state.activityStack.length - 1];
    const el = $("activity-toast");
    const text = $("activity-toast-text");
    if (text) text.textContent = top.message;
    if (el) el.hidden = false;
  };
  if (state.activityShowTimer) clearTimeout(state.activityShowTimer);
  // If something is already visible, update immediately; otherwise delay to avoid flicker
  const toast = $("activity-toast");
  if (toast && !toast.hidden) {
    show();
  } else {
    state.activityShowTimer = setTimeout(show, delayMs);
  }
  return token;
}

function endActivity(token) {
  if (token == null) return;
  state.activityStack = state.activityStack.filter((x) => x.token !== token);
  if (!state.activityStack.length) {
    if (state.activityShowTimer) {
      clearTimeout(state.activityShowTimer);
      state.activityShowTimer = null;
    }
    const el = $("activity-toast");
    if (el) el.hidden = true;
    return;
  }
  const top = state.activityStack[state.activityStack.length - 1];
  const text = $("activity-toast-text");
  if (text) text.textContent = top.message;
}

async function withActivity(message, fn) {
  const token = beginActivity(message);
  try {
    return await fn();
  } finally {
    endActivity(token);
  }
}

function $(id) {
  return document.getElementById(id);
}

/** Typed confirm phrases — must match app/services/safety.py */
const CONFIRM_DELETE_ICLOUD = "DELETE FROM ICLOUD";
const CONFIRM_DELETE_FOREVER = "DELETE FOREVER";
const CONFIRM_CLEANUP = "DELETE FILES";

/**
 * Prompt for an exact confirm phrase. Returns the phrase or null if cancelled/mismatch.
 * @param {string} phrase
 * @param {string} intro
 */
function promptConfirmToken(phrase, intro) {
  const typed = window.prompt(`${intro}\n\nType ${phrase} to confirm:`);
  if (typed == null) return null;
  if (typed.trim() !== phrase) {
    alert(`Confirmation text must be exactly:\n${phrase}`);
    return null;
  }
  return phrase;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

/**
 * Shared progress bar updater for long jobs.
 * @param {HTMLElement|null} barEl
 * @param {HTMLElement|null} fillEl
 * @param {{ processed?: number, total?: number, indeterminate?: boolean, visible?: boolean }} opts
 */
function updateJobProgress(barEl, fillEl, opts = {}) {
  if (!barEl) return;
  const processed = Number(opts.processed) || 0;
  const total = Number(opts.total) || 0;
  const indeterminate = !!opts.indeterminate || (opts.visible && total <= 0);
  const visible = opts.visible !== undefined ? !!opts.visible : true;
  barEl.hidden = !visible;
  barEl.classList.toggle("indeterminate", indeterminate && visible);
  if (!fillEl) return;
  if (!visible) {
    fillEl.style.width = "0%";
    return;
  }
  if (indeterminate || total <= 0) {
    fillEl.style.width = "35%";
    return;
  }
  const pct = Math.min(100, Math.max(0, Math.round((100 * processed) / total)));
  fillEl.style.width = `${pct}%`;
}

function formatJobCounts(active) {
  if (!active) return "";
  const unit = active.unit || "items";
  const processed = Number(active.processed) || 0;
  const total = Number(active.total) || 0;
  if (active.cancelling) return "Finishing current step…";
  if (active.paused) {
    if (total > 0) {
      return `Paused at ${processed.toLocaleString()} / ${total.toLocaleString()} ${unit}`;
    }
    if (processed > 0) return `Paused after ${processed.toLocaleString()} ${unit}`;
    return "Paused";
  }
  if (total > 0) {
    return `${processed.toLocaleString()} / ${total.toLocaleString()} ${unit}`;
  }
  if (processed > 0) {
    return `${processed.toLocaleString()} ${unit} so far`;
  }
  return active.phase_label || "Starting…";
}

function formatJobProgressDetail(active) {
  if (!active) return "";
  const parts = [];
  if (active.phase_label && !active.cancelling && !active.paused) {
    parts.push(active.phase_label);
  }
  if (active.cancelling) parts.push("Cancelling…");
  else if (active.paused) parts.push("Paused");
  if (active.eta_label) parts.push(active.eta_label);
  if (active.rate_label) parts.push(active.rate_label);
  if (active.stats) parts.push(active.stats);
  if (active.current) parts.push(`now: ${active.current}`);
  if (active.detail) {
    // Avoid duplicating the same text already shown in counts/phase
    const detail = String(active.detail);
    if (!parts.includes(detail)) parts.push(detail);
  }
  return parts.join(" · ") || "One database-heavy job at a time";
}

function updateBusyBannerProgress(active) {
  const pctEl = $("job-busy-pct");
  const countsEl = $("job-busy-counts");
  if (countsEl) countsEl.textContent = formatJobCounts(active);
  if (!pctEl) return;
  if (!active || active.cancelling) {
    pctEl.textContent = "…";
    pctEl.classList.add("is-indet");
    return;
  }
  if (active.percent != null && !active.indeterminate) {
    pctEl.textContent = `${active.percent}%`;
    pctEl.classList.remove("is-indet");
    return;
  }
  pctEl.textContent = "…";
  pctEl.classList.add("is-indet");
}

function showJobWait(title, detail) {
  const overlay = $("job-wait-overlay");
  const titleEl = $("job-wait-title");
  const detailEl = $("job-wait-detail");
  if (titleEl) titleEl.textContent = title || "Please wait";
  if (detailEl) {
    detailEl.textContent =
      detail || "Finishing the current file operation… tabs unlock when this completes.";
  }
  if (overlay) overlay.hidden = false;
  state.jobWaitVisible = true;
}

function hideJobWait() {
  const overlay = $("job-wait-overlay");
  if (overlay) overlay.hidden = true;
  state.jobWaitVisible = false;
}

async function requestJobCancel(url, label) {
  if (!url) return;
  showJobWait(
    `Cancelling ${label || "job"}…`,
    "Please wait — over SMB this can take a bit while the current file finishes."
  );
  try {
    await api(url, { method: "POST", body: "{}" });
  } catch (e) {
    hideJobWait();
    alert(e.message);
    return;
  }
  await pollBusyJobs();
  pollScan();
  pollImport();
  if (typeof pollArchive === "function") pollArchive();
  if (typeof pollInventory === "function") pollInventory();
  if (typeof pollExactBatch === "function") pollExactBatch();
  if (typeof pollOrganize === "function") pollOrganize();
  if (typeof pollCleanup === "function") pollCleanup();
  if (typeof pollRawConvert === "function") pollRawConvert();
  if (typeof pollFaceScan === "function") pollFaceScan();
}

/* Heavy-job coordination (scan / import / inventory / archive / dedupe batch) */
const HEAVY_START_IDS = [
  "btn-scan",
  "btn-scan-full",
  "btn-import-start",
  "btn-icloud-inventory",
  "btn-archive-start",
  "btn-dupe-batch-start",
  "btn-org-all",
  "btn-move",
  "btn-cleanup-start",
  "btn-cleanup-preview",
  "btn-raw-start",
  "btn-raw-preview",
  "btn-faces-scan",
  "btn-faces-cluster",
  "btn-faces-preview",
];

/** Tabs that stay usable for any heavy job (read-only / settings). */
const ALWAYS_SAFE_VIEWS = new Set(["browse", "map", "settings"]);

/** Which nav tab belongs to each heavy job (kept open so you can watch progress). */
const JOB_VIEW = {
  scan: "scan",
  import: "import",
  inventory: "archive",
  archive: "archive",
  dedupe_batch: "duplicates",
  cleanup: "settings",
  organize: "organize",
  raw_convert: "converter",
  face_scan: "people",
};

function setHeavyStartsDisabled(disabled) {
  for (const id of HEAVY_START_IDS) {
    const el = $(id);
    if (!el) continue;
    if (el.dataset.defaultTitle == null) {
      el.dataset.defaultTitle = el.title || "";
    }
    el.disabled = !!disabled;
    el.title = disabled
      ? "Another database job is running — finish or cancel it first"
      : el.dataset.defaultTitle;
  }
}

function allowedViewsForJob(jobId) {
  const allowed = new Set(ALWAYS_SAFE_VIEWS);
  if (jobId && JOB_VIEW[jobId]) allowed.add(JOB_VIEW[jobId]);
  return allowed;
}

function updateNavForBusy(busy, jobId) {
  const allowed = busy ? allowedViewsForJob(jobId) : null;
  const jobView = jobId ? JOB_VIEW[jobId] : null;

  document.querySelectorAll(".nav-item").forEach((btn) => {
    const view = btn.dataset.view;
    btn.classList.remove("nav-safe", "nav-blocked", "nav-active-job");

    if (!busy) {
      btn.disabled = false;
      btn.removeAttribute("aria-disabled");
      btn.title = "";
      return;
    }

    const ok = allowed.has(view);
    btn.disabled = !ok;
    btn.setAttribute("aria-disabled", ok ? "false" : "true");
    if (ok) {
      btn.classList.add("nav-safe");
      if (view === jobView) btn.classList.add("nav-active-job");
      btn.title =
        view === jobView
          ? "Active job — watch progress or cancel here"
          : "Available while the job runs";
    } else {
      btn.classList.add("nav-blocked");
      btn.title = "Paused while another database job is running";
    }
  });

  document.body.classList.toggle("jobs-busy", !!busy);

  // If current view is blocked, jump to the job tab (or Browse)
  if (busy && state.view && !allowed.has(state.view)) {
    const fallback = jobView || "browse";
    const target = document.querySelector(`.nav-item[data-view="${fallback}"]`);
    if (target && !target.disabled) target.click();
  }
}

async function pollBusyJobs() {
  try {
    const s = await api("/api/jobs/busy");
    const banner = $("job-busy-banner");
    const title = $("job-busy-title");
    const detail = $("job-busy-detail");
    const cancelBtn = $("btn-job-busy-cancel");
    const pauseBtn = $("btn-job-busy-pause");
    const resumeBtn = $("btn-job-busy-resume");
    const hint = document.querySelector(".job-busy-hint");
    const busyBar = $("job-busy-bar");
    const busyFill = $("job-busy-bar-fill");
    const active = s.active;
    state.busyJobId = active?.id || null;

    if (banner) banner.hidden = !s.busy;
    if (s.busy && active) {
      if (title) {
        title.textContent = active.cancelling
          ? `Cancelling ${active.label}…`
          : active.paused
            ? `${active.label} paused`
            : active.phase_label
              ? `${active.label} · ${active.phase_label}`
              : `${active.label} running`;
      }
      updateBusyBannerProgress(active);
      if (detail) detail.textContent = formatJobProgressDetail(active);
      updateJobProgress(busyBar, busyFill, {
        processed: active.processed,
        total: active.total,
        indeterminate: !!active.indeterminate || !!active.cancelling,
        visible: true,
      });
      if (active.cancelling) {
        showJobWait(
          `Cancelling ${active.label}…`,
          active.detail ||
            "Waiting for the current file check/move to finish. Tabs unlock when cancel completes."
        );
      } else if (state.jobWaitVisible && !active.cancelling) {
        // Keep overlay only while cancelling
      }
      if (hint) {
        const jobTab = JOB_VIEW[active.id];
        const tabLabel =
          jobTab === "scan"
            ? "Scan"
            : jobTab === "archive"
              ? "Archive"
              : jobTab === "import"
                ? "Import"
                : jobTab === "duplicates"
                  ? "Duplicates"
                  : jobTab === "organize"
                    ? "Organize"
                    : jobTab === "converter"
                      ? "File Converter"
                      : jobTab === "people"
                        ? "People"
                        : jobTab === "settings"
                          ? "Settings"
                          : "Browse";
        hint.textContent = active.cancelling
          ? "Cancel in progress — please wait"
          : active.paused
            ? `Paused · Resume anytime · ${tabLabel} stays open`
            : `Available: Browse, Map, Settings, ${tabLabel} · other tabs paused`;
      }
      if (pauseBtn) {
        pauseBtn.hidden = !active.pause || !!active.cancelling || !!active.paused;
        pauseBtn.disabled = !!active.cancelling;
        pauseBtn.dataset.pauseUrl = active.pause || "";
      }
      if (resumeBtn) {
        resumeBtn.hidden = !active.resume || !!active.cancelling || !active.paused;
        resumeBtn.disabled = !!active.cancelling;
        resumeBtn.dataset.resumeUrl = active.resume || "";
      }
      if (cancelBtn) {
        cancelBtn.hidden = !active.cancel || !!active.cancelling;
        cancelBtn.disabled = !!active.cancelling;
        cancelBtn.dataset.cancelUrl = active.cancel || "";
        cancelBtn.textContent = active.cancelling ? "Cancelling…" : "Cancel job";
      }
      setHeavyStartsDisabled(true);
      updateNavForBusy(true, active.id);
    } else {
      if (title) title.textContent = "Job running";
      if (detail) detail.textContent = "";
      updateBusyBannerProgress(null);
      updateJobProgress(busyBar, busyFill, { visible: false });
      hideJobWait();
      if (hint) {
        hint.textContent = "Browse & Map stay available · other start actions paused";
      }
      if (pauseBtn) {
        pauseBtn.hidden = true;
        pauseBtn.dataset.pauseUrl = "";
      }
      if (resumeBtn) {
        resumeBtn.hidden = true;
        resumeBtn.dataset.resumeUrl = "";
      }
      if (cancelBtn) {
        cancelBtn.hidden = true;
        cancelBtn.disabled = false;
        cancelBtn.dataset.cancelUrl = "";
        cancelBtn.textContent = "Cancel job";
      }
      setHeavyStartsDisabled(false);
      updateNavForBusy(false, null);
    }

    if (s.busy) {
      if (!state.busyPoll) state.busyPoll = setInterval(pollBusyJobs, 1500);
    } else if (state.busyPoll) {
      clearInterval(state.busyPoll);
      state.busyPoll = setInterval(pollBusyJobs, 4000);
    }
  } catch {
    /* ignore transient errors */
  }
}

$("btn-job-busy-cancel")?.addEventListener("click", async () => {
  const btn = $("btn-job-busy-cancel");
  const url = btn?.dataset.cancelUrl;
  const label = $("job-busy-title")?.textContent?.replace(/\s+(running|paused)$/, "") || "job";
  await requestJobCancel(url, label);
});

$("btn-job-busy-pause")?.addEventListener("click", async () => {
  const btn = $("btn-job-busy-pause");
  const url = btn?.dataset.pauseUrl;
  if (!url) return;
  try {
    await api(url, { method: "POST", body: "{}" });
    pollBusyJobs();
    if (typeof pollFaceScan === "function") pollFaceScan();
    if (typeof pollOrganize === "function") pollOrganize();
    if (typeof pollRawConvert === "function") pollRawConvert();
    if (typeof pollScan === "function") pollScan();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-job-busy-resume")?.addEventListener("click", async () => {
  const btn = $("btn-job-busy-resume");
  const url = btn?.dataset.resumeUrl;
  if (!url) return;
  try {
    await api(url, { method: "POST", body: "{}" });
    pollBusyJobs();
    if (typeof pollFaceScan === "function") pollFaceScan();
    if (typeof pollOrganize === "function") pollOrganize();
    if (typeof pollRawConvert === "function") pollRawConvert();
    if (typeof pollScan === "function") pollScan();
  } catch (e) {
    alert(e.message);
  }
});

/* Navigation */
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return;
    state.view = btn.dataset.view;
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${state.view}`));
    if (state.view === "browse") loadPhotos();
    if (state.view === "map") loadMap();
    if (state.view === "settings") loadSettings();
    if (state.view === "archive") refreshIcloudStatus();
    if (state.view === "people") loadPeopleView();
  });
});

/* Stats + scan */
async function refreshStats() {
  try {
    const s = await api("/api/stats");
    $("stat-count").textContent = s.photo_count.toLocaleString();
    $("stat-folders").textContent = s.folder_count.toLocaleString();
    const sizeEl = $("stat-size");
    if (sizeEl) sizeEl.textContent = formatBytes(s.total_bytes || 0);
    const volEl = $("stat-volume");
    if (volEl) {
      volEl.textContent =
        s.volume_used_bytes != null ? formatBytes(s.volume_used_bytes) : "—";
    }
  } catch {
    /* ignore until configured */
  }
}

function formatElapsed(seconds) {
  if (seconds == null) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

const RING_LEN = 2 * Math.PI * 52; // ~326.73
const MINI_LEN = 2 * Math.PI * 15.5; // ~97.4

function setRingProgress(pct, indeterminate) {
  const ring = $("scan-ring-fg");
  const mini = $("scan-mini-fg");
  const hero = document.querySelector(".scan-hero");
  if (hero) {
    hero.classList.toggle("indeterminate", !!indeterminate);
  }
  if (ring) {
    if (indeterminate) {
      ring.style.strokeDashoffset = "";
    } else {
      const p = Math.max(0, Math.min(100, pct ?? 0));
      ring.style.strokeDashoffset = String(RING_LEN * (1 - p / 100));
    }
  }
  if (mini) {
    const p = indeterminate ? 15 : Math.max(0, Math.min(100, pct ?? 0));
    mini.style.strokeDashoffset = String(MINI_LEN * (1 - p / 100));
  }
}

function ensureFilmFrames() {
  const track = $("scan-film-track");
  if (!track || track.childElementCount) return;
  for (let i = 0; i < 48; i++) {
    const f = document.createElement("div");
    f.className = "frame";
    track.appendChild(f);
  }
}

function goToScan() {
  const btn = document.querySelector('.nav-item[data-view="scan"]');
  if (btn) btn.click();
}

async function pollScan() {
  try {
    const s = await api("/api/scan/status");
    const bar = $("scan-bar-track");
    const barFill = $("scan-bar-fill");
    const meta = $("scan-meta");
    const warn = $("scan-warn");
    const btnScan = $("btn-scan");
    const hero = document.querySelector(".scan-hero");
    if (!bar || !barFill) return;

    ensureFilmFrames();

    const running = s.status === "running";
    const scanning = running && (s.phase === "scanning" || s.phase === "discovering" || s.phase === "indexing" || s.phase === "finishing");
    if (hero) hero.classList.toggle("running", running);

    // total grows during the walk (we set total = max(total, processed)), so
    // processed === total does NOT mean done — only finishing/completed does.
    const stillWalking = running && s.phase === "scanning";
    const haveTotal = s.total && s.total > 0;
    const atMovingCeiling = stillWalking && haveTotal && s.processed >= s.total;
    const pct =
      haveTotal && !atMovingCeiling
        ? Math.min(99, Math.round((s.processed / s.total) * 100)) // cap at 99% while running
        : haveTotal && !running
          ? Math.min(100, Math.round((s.processed / s.total) * 100))
          : null;
    const indeterminate =
      running && (!haveTotal || atMovingCeiling || s.processed > s.total || stillWalking && !s.prior_total);

    // If we have a prior full-library estimate larger than processed, show real %
    let displayPct = pct;
    if (stillWalking && s.prior_total && s.prior_total > s.processed) {
      displayPct = Math.min(99, Math.round((s.processed / s.prior_total) * 100));
    }
    const useIndeterminate = indeterminate && displayPct == null;

    if (useIndeterminate) {
      bar.classList.add("indeterminate");
      barFill.style.width = "35%";
      setRingProgress(15, true);
    } else {
      bar.classList.remove("indeterminate");
      const p = displayPct ?? (running ? 0 : 0);
      const showP = s.status === "completed" ? Math.min(100, Math.round((s.processed / (s.total || s.processed || 1)) * 100)) : p;
      barFill.style.width = `${running || s.status === "completed" ? showP : 0}%`;
      setRingProgress(s.status === "idle" && !s.resumable ? 0 : showP, false);
    }

    const pctEl = $("scan-pct");
    const pctLabel = $("scan-pct-label");
    const miniPct = $("scan-mini-pct");
    if (useIndeterminate || (stillWalking && (atMovingCeiling || displayPct == null))) {
      const n = s.processed || 0;
      const label = n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
      if (pctEl) pctEl.textContent = label;
      if (pctLabel) pctLabel.textContent = "found so far";
      if (miniPct) miniPct.textContent = label;
    } else if (displayPct != null && (running || s.status === "completed" || s.resumable)) {
      const shown = s.status === "completed" ? 100 : displayPct;
      if (pctEl) pctEl.textContent = `${shown}%`;
      if (pctLabel) pctLabel.textContent = running ? "of estimate" : s.status === "completed" ? "done" : "saved";
      if (miniPct) miniPct.textContent = `${shown}%`;
    } else {
      if (pctEl) pctEl.textContent = "—";
      if (pctLabel) pctLabel.textContent = "ready";
      if (miniPct) miniPct.textContent = "—";
    }

    let headline = "Index your My Cloud photos";
    let detail =
      "Seen = media files checked this run. New in app = just indexed (browseable). Drive used is the whole Z: volume (includes non-photos). Remaining work is unknown until the walk finishes.";
    let statusShort = "Idle";

    if (s.status === "idle") {
      if (s.resumable) {
        headline = "Ready to resume";
        detail = `${(s.already_indexed || 0).toLocaleString()} already in the app (${formatBytes(s.library_bytes || 0)} indexed). Resume skips unchanged files.`;
        statusShort = "Resume ready";
      } else if (s.library_count) {
        detail = `${(s.library_count || 0).toLocaleString()} in app · ${formatBytes(s.library_bytes || 0)} indexed` +
          (s.volume_used_bytes != null
            ? ` · drive ${formatBytes(s.volume_used_bytes)} used of ${formatBytes(s.volume_total_bytes || 0)}`
            : "");
      }
    } else if (s.status === "error") {
      const short = (s.message || "error").split("\n")[0];
      headline = "Scan interrupted";
      detail = `${short} — progress was saved. Click Resume scan.`;
      statusShort = "Error — resume";
    } else if (s.status === "cancelled") {
      headline = "Scan cancelled";
      detail = s.message || "Click Resume scan to continue where you left off.";
      statusShort = "Cancelled";
    } else if (scanning) {
      headline = s.message || "Scanning library…";
      const dataBit =
        s.bytes_processed > 0 ? ` · ${formatBytes(s.bytes_processed)} this run` : "";
      detail = stillWalking
        ? `${(s.processed || 0).toLocaleString()} media files found so far — still walking the drive${dataBit}`
        : haveTotal
          ? `${(s.processed || 0).toLocaleString()} of ${s.total.toLocaleString()} files${dataBit}`
          : `${(s.processed || 0).toLocaleString()} files handled so far${dataBit}`;
      statusShort = stillWalking
        ? `${(s.processed || 0).toLocaleString()} found`
        : displayPct != null
          ? `${displayPct}%`
          : "Scanning…";
    } else if (s.status === "completed") {
      headline = "Scan complete";
      detail = s.message || "Library index is up to date.";
      statusShort = "Done";
    }

    const hl = $("scan-headline");
    const det = $("scan-detail");
    const st = $("scan-status");
    const miniLabel = $("scan-mini-label");
    if (hl) hl.textContent = headline;
    if (det) det.textContent = detail;
    if (st) st.textContent = statusShort;
    if (miniLabel) miniLabel.textContent = running ? "Scanning" : s.resumable ? "Resume" : "Scan";

    if (!running && btnScan) {
      btnScan.textContent = s.resumable ? "Resume scan" : "Scan library";
    }

    const metaBits = [];
    if (running && s.eta_label) metaBits.push(`ETA ${s.eta_label}`);
    if (running && s.bytes_processed) metaBits.push(formatBytes(s.bytes_processed));
    if (running && s.skipped != null) metaBits.push(`skip ${(s.skipped || 0).toLocaleString()}`);
    if (running && s.rate_per_sec != null) metaBits.push(`${s.rate_per_sec}/s`);
    if (running && s.rate_bytes_per_sec != null) {
      metaBits.push(`${(s.rate_bytes_per_sec / (1024 * 1024)).toFixed(1)} MB/s`);
    }
    if (running && s.elapsed_seconds != null) metaBits.push(`elapsed ${formatElapsed(s.elapsed_seconds)}`);
    if (s.library_bytes != null) metaBits.push(`indexed ${formatBytes(s.library_bytes)}`);
    if (s.volume_used_bytes != null && s.volume_total_bytes != null) {
      metaBits.push(
        `drive ${formatBytes(s.volume_used_bytes)} / ${formatBytes(s.volume_total_bytes)}`
      );
    }
    if (!running && s.status === "completed" && s.elapsed_seconds != null) {
      metaBits.push(`Took ${formatElapsed(s.elapsed_seconds)}`);
    }
    if (!running && s.resumable && s.already_indexed) {
      metaBits.push(`${s.already_indexed.toLocaleString()} saved in index`);
    }
    if (metaBits.length) {
      meta.hidden = false;
      meta.textContent = metaBits.join(" · ");
    } else {
      meta.hidden = true;
      meta.textContent = "";
    }

    const showWarn =
      running &&
      (s.long_scan ||
        (s.eta_seconds != null && s.eta_seconds >= 300) ||
        (s.estimate_seconds != null && s.estimate_seconds >= 300));
    warn.hidden = !showWarn;
    if (showWarn) {
      const label = s.eta_label || s.estimate_label;
      warn.textContent = label
        ? `Large library — about ${label} remaining`
        : "Large library — this may take a while (safe to resume if interrupted)";
    }

    const setTile = (id, val) => {
      const el = $(id);
      if (el) el.textContent = val;
    };
    setTile("tile-processed", (s.processed || 0).toLocaleString());
    setTile("tile-added", (s.added || 0).toLocaleString());
    setTile("tile-data", s.bytes_processed ? formatBytes(s.bytes_processed) : "—");
    setTile("tile-library", s.library_bytes != null ? formatBytes(s.library_bytes) : "—");
    setTile(
      "tile-volume",
      s.volume_used_bytes != null && s.volume_total_bytes != null
        ? `${formatBytes(s.volume_used_bytes)} / ${formatBytes(s.volume_total_bytes)}`
        : "—"
    );
    setTile("tile-skipped", (s.skipped || 0).toLocaleString());
    setTile("tile-updated", (s.updated || 0).toLocaleString());
    setTile("tile-errors", (s.errors || 0).toLocaleString());
    setTile("tile-eta", running ? s.eta_label || "…" : "—");
    setTile("tile-rate", s.rate_per_sec != null ? String(s.rate_per_sec) : "—");
    setTile(
      "tile-rate-bytes",
      s.rate_bytes_per_sec != null
        ? (s.rate_bytes_per_sec / (1024 * 1024)).toFixed(1)
        : "—"
    );
    setTile("tile-elapsed", s.elapsed_seconds != null ? formatElapsed(s.elapsed_seconds) : "—");

    if (btnScan) btnScan.disabled = running || !!state.busyJobId;
    $("btn-scan-full").disabled = running || !!state.busyJobId;
    $("btn-scan-cancel").hidden = !running;

    if (running) {
      if (!state.pollTimer) state.pollTimer = setInterval(pollScan, 1000);
      pollBusyJobs();
    } else if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      refreshStats();
      if (state.view === "browse") loadPhotos();
      loadFilters();
      pollBusyJobs();
    }
  } catch (e) {
    const el = $("scan-status");
    if (el) el.textContent = e.message;
  }
}

$("btn-scan").addEventListener("click", async () => {
  try {
    goToScan();
    await api("/api/scan/start", { method: "POST", body: JSON.stringify({ full: false }) });
    pollScan();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-scan-full").addEventListener("click", async () => {
  if (!confirm("Full scan rehashes every file. Continue?")) return;
  try {
    goToScan();
    await api("/api/scan/start", { method: "POST", body: JSON.stringify({ full: true }) });
    pollScan();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-scan-cancel").addEventListener("click", async () => {
  try {
    await api("/api/scan/cancel", { method: "POST", body: "{}" });
    pollScan();
  } catch (e) {
    alert(e.message);
  }
});

$("scan-mini")?.addEventListener("click", goToScan);

/* Browse */
async function loadFilters() {
  try {
    const [folders, timeline, smart, names, people] = await Promise.all([
      api("/api/folders"),
      api("/api/timeline"),
      api("/api/smart-folders"),
      api("/api/names?limit=300"),
      api("/api/people"),
    ]);
    const ff = $("folder-filter");
    const cur = ff.value;
    ff.innerHTML = `<option value="">All folders</option>`;
    for (const f of folders.folders) {
      const label = f.folder || "(root)";
      const opt = document.createElement("option");
      opt.value = f.folder;
      opt.textContent = `${label} (${f.count})`;
      ff.appendChild(opt);
    }
    ff.value = cur;

    const years = new Set();
    const mf = $("month-filter");
    const mcur = mf.value;
    mf.innerHTML = `<option value="">Added · all months</option>`;
    for (const m of timeline.months) {
      const opt = document.createElement("option");
      opt.value = m.month;
      opt.textContent = `${m.month} (${m.count})`;
      mf.appendChild(opt);
      if (m.month && m.month.length >= 4) years.add(m.month.slice(0, 4));
    }
    mf.value = mcur;

    const yf = $("year-filter");
    if (yf) {
      const ycur = yf.value;
      yf.innerHTML = `<option value="">Added · all years</option>`;
      for (const y of [...years].sort().reverse()) {
        const opt = document.createElement("option");
        opt.value = y;
        opt.textContent = y;
        yf.appendChild(opt);
      }
      yf.value = ycur;
    }

    const sortEl = $("sort-filter");
    if (sortEl && state.sort) sortEl.value = state.sort;

    state.nameBrowseList = names.names || [];
    state.peopleBrowseList = people.people || [];
    renderPeopleBrowse();
    renderNameBrowse();
    renderSmartFolders(smart.folders || []);
  } catch {
    /* empty library */
  }
}

function renderPeopleBrowse() {
  const list = $("people-browse-list");
  if (!list) return;
  const filterEl = $("people-browse-filter");
  const needle = (filterEl?.value || "").trim().toLowerCase();
  const activeId = state.personId;
  let people = state.peopleBrowseList || [];
  if (needle) {
    people = people.filter((p) => String(p.name || "").toLowerCase().includes(needle));
  }
  list.innerHTML = "";

  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "smart-folder-btn" + (activeId == null ? " active" : "");
  clearBtn.innerHTML = `<span>All people</span>`;
  clearBtn.addEventListener("click", () => setPersonBrowse(null));
  list.appendChild(clearBtn);

  if (!people.length) {
    const empty = document.createElement("div");
    empty.className = "name-browse-empty";
    empty.textContent = needle
      ? "No matching people"
      : "No named faces yet — name people under People";
    list.appendChild(empty);
    return;
  }

  for (const p of people.slice(0, 80)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "smart-folder-btn" + (activeId != null && Number(activeId) === Number(p.id) ? " active" : "");
    btn.title = `Face-tagged photos of ${p.name}`;
    btn.innerHTML = `<span>${escapeHtml(p.name)}</span><span class="count">${Number(
      p.photo_count || 0
    ).toLocaleString()}</span>`;
    btn.addEventListener("click", () => setPersonBrowse(p.id));
    list.appendChild(btn);
  }
}

function setPersonBrowse(personId) {
  state.personId = personId != null && personId !== "" ? Number(personId) : null;
  state.nameBrowse = "";
  state.smartFolder = "";
  state.offset = 0;
  state.selected.clear();
  updateSelectionCount();
  const sel = $("smart-filter");
  if (sel) sel.value = "";
  const search = $("search");
  if (search) search.value = "";
  const folder = $("folder-filter");
  if (folder) folder.value = "";
  const qbar = $("quarantine-bar");
  if (qbar) qbar.hidden = true;
  renderPeopleBrowse();
  renderNameBrowse();
  document.querySelectorAll("#smart-folder-list .smart-folder-btn").forEach((b) => {
    const match = (b.dataset.id || "") === state.smartFolder;
    b.classList.toggle("active", match || (!state.smartFolder && !b.dataset.id));
  });
  loadPhotos();
}

function renderNameBrowse() {
  const list = $("name-browse-list");
  if (!list) return;
  const filterEl = $("name-browse-filter");
  const needle = (filterEl?.value || "").trim().toLowerCase();
  const active = (state.nameBrowse || "").trim();
  let names = state.nameBrowseList || [];
  if (needle) {
    names = names.filter((n) => String(n.name || "").toLowerCase().includes(needle));
  }
  list.innerHTML = "";

  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "smart-folder-btn" + (!active ? " active" : "");
  clearBtn.innerHTML = `<span>All names</span>`;
  clearBtn.addEventListener("click", () => setNameBrowse(""));
  list.appendChild(clearBtn);

  if (!names.length) {
    const empty = document.createElement("div");
    empty.className = "name-browse-empty";
    empty.textContent = needle ? "No matching names" : "No folder names yet — run a library scan";
    list.appendChild(empty);
    return;
  }

  for (const n of names.slice(0, 80)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "smart-folder-btn" +
      (active.toLowerCase() === String(n.name || "").toLowerCase() ? " active" : "");
    btn.title = `Photos whose path contains “${n.name}”`;
    btn.innerHTML = `<span>${escapeHtml(n.name)}</span><span class="count">${Number(
      n.count || 0
    ).toLocaleString()}</span>`;
    btn.addEventListener("click", () => setNameBrowse(n.name));
    list.appendChild(btn);
  }
}

function setNameBrowse(name) {
  const next = (name || "").trim();
  state.nameBrowse = next;
  state.personId = null;
  state.smartFolder = "";
  state.offset = 0;
  state.selected.clear();
  updateSelectionCount();
  const sel = $("smart-filter");
  if (sel) sel.value = "";
  const search = $("search");
  if (search) search.value = next;
  const folder = $("folder-filter");
  if (folder) folder.value = "";
  const qbar = $("quarantine-bar");
  if (qbar) qbar.hidden = true;
  renderPeopleBrowse();
  renderNameBrowse();
  document.querySelectorAll("#smart-folder-list .smart-folder-btn").forEach((b) => {
    const match = (b.dataset.id || "") === state.smartFolder;
    b.classList.toggle("active", match || (!state.smartFolder && !b.dataset.id));
  });
  loadPhotos();
}

function renderSmartFolders(folders) {
  const list = $("smart-folder-list");
  const sel = $("smart-filter");
  if (!list || !sel) return;

  const cur = state.smartFolder;
  sel.innerHTML = `<option value="">All media</option>`;
  list.innerHTML = "";

  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "smart-folder-btn" + (!cur ? " active" : "");
  allBtn.innerHTML = `<span>All media</span>`;
  allBtn.addEventListener("click", () => setSmartFolder(""));
  list.appendChild(allBtn);

  for (const f of folders) {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = `${f.name} (${f.count})`;
    sel.appendChild(opt);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "smart-folder-btn" +
      (cur === f.id ? " active" : "") +
      (f.id === "quarantine" ? " quarantine" : "");
    btn.dataset.id = f.id;
    btn.innerHTML = `<span>${escapeHtml(f.name)}</span><span class="count">${f.count}</span>`;
    btn.addEventListener("click", () => setSmartFolder(f.id));
    list.appendChild(btn);
  }
  sel.value = cur;
}

function updateSelectionCount() {
  const n = state.selected.size;
  const label = `${n.toLocaleString()} selected`;
  const selCount = $("sel-count");
  if (selCount) selCount.textContent = String(n);
  const qCount = $("quarantine-sel-count");
  if (qCount) {
    qCount.textContent = label;
    qCount.classList.toggle("has-selection", n > 0);
  }
  const browseCount = $("browse-sel-count");
  if (browseCount) {
    browseCount.textContent = label;
    browseCount.classList.toggle("has-selection", n > 0);
  }
  const inQuarantine = state.smartFolder === "quarantine";
  const browseBar = $("browse-sel-bar");
  if (browseBar) browseBar.hidden = inQuarantine || n === 0;
  const selectAllBtn = $("btn-q-select-all");
  if (selectAllBtn && inQuarantine) {
    const visible = document.querySelectorAll("#photo-grid .card .check").length;
    const allOn =
      visible > 0 &&
      [...document.querySelectorAll("#photo-grid .card .check")].every((c) => c.checked);
    selectAllBtn.textContent = allOn ? "All selected" : "Select all";
  }
  const browseSelectAll = $("btn-browse-select-all");
  if (browseSelectAll && !inQuarantine) {
    const visible = document.querySelectorAll("#photo-grid .card .check").length;
    const allOn =
      visible > 0 &&
      [...document.querySelectorAll("#photo-grid .card .check")].every((c) => c.checked);
    browseSelectAll.textContent = allOn ? "All on page selected" : "Select all on page";
  }
}

function selectAllBrowseVisible(selected) {
  const grid = $("photo-grid");
  if (!grid) return;
  for (const card of grid.querySelectorAll(".card")) {
    const check = card.querySelector(".check");
    if (!check || check.dataset.id == null) continue;
    const id = Number(check.dataset.id);
    check.checked = selected;
    card.classList.toggle("selected", selected);
    if (selected) state.selected.add(id);
    else state.selected.delete(id);
  }
  updateSelectionCount();
}

function setSmartFolder(id) {
  state.smartFolder = id || "";
  state.nameBrowse = "";
  state.personId = null;
  state.offset = 0;
  state.selected.clear();
  updateSelectionCount();
  const sel = $("smart-filter");
  if (sel) sel.value = state.smartFolder;
  const search = $("search");
  if (search && state.smartFolder) search.value = "";
  document.querySelectorAll("#smart-folder-list .smart-folder-btn").forEach((b) => {
    const match = (b.dataset.id || "") === state.smartFolder;
    b.classList.toggle("active", match || (!state.smartFolder && !b.dataset.id));
  });
  renderPeopleBrowse();
  renderNameBrowse();
  const qbar = $("quarantine-bar");
  if (qbar) qbar.hidden = state.smartFolder !== "quarantine";
  loadPhotos();
}

async function loadPhotos() {
  const folder = $("folder-filter").value;
  const monthVal = $("month-filter").value;
  const yearFilter = $("year-filter")?.value || "";
  let year = yearFilter || null;
  let month = null;
  if (monthVal && monthVal.includes("-")) {
    const parts = monthVal.split("-");
    year = parts[0];
    month = parts[1];
  }
  const q = $("search").value.trim();
  const sort = $("sort-filter")?.value || state.sort || "date_desc";
  state.sort = sort;
  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
  });

  const inQuarantine = state.smartFolder === "quarantine";
  const qbar = $("quarantine-bar");
  if (qbar) qbar.hidden = !inQuarantine;
  const browseBar = $("browse-sel-bar");
  if (browseBar) browseBar.hidden = inQuarantine || state.selected.size === 0;

  const grid = $("photo-grid");
  grid.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    let data;
    if (state.smartFolder) {
      data = await api(
        `/api/smart-folders/${encodeURIComponent(state.smartFolder)}/photos?${params}`
      );
    } else {
      if (folder) params.set("folder", folder);
      if (year) params.set("year", year);
      if (month) params.set("month", month);
      if (q) params.set("q", q);
      if (state.personId != null) params.set("person_id", String(state.personId));
      if (sort) params.set("sort", sort);
      data = await api(`/api/photos?${params}`);
    }
    state.total = data.total;
    if (inQuarantine && data.quarantine_dir) {
      const pathEl = $("quarantine-bar-path");
      if (pathEl) pathEl.textContent = data.quarantine_dir;
    }
    $("pager-label").textContent = data.total
      ? `${state.offset + 1}–${Math.min(state.offset + data.items.length, data.total)} of ${data.total}`
      : "No photos";
    $("btn-prev").disabled = state.offset <= 0;
    $("btn-next").disabled = state.offset + state.limit >= data.total;

    if (!data.items.length) {
      grid.innerHTML = `<div class="empty">${
        inQuarantine
          ? "Quarantine is empty. Duplicates you quarantine will show up here."
          : "No photos yet. Set PHOTO_ROOT in Settings and run Scan."
      }</div>`;
      return;
    }

    grid.innerHTML = "";
    for (const p of data.items) {
      const isVideo = (p.media_type || "image") === "video" || isVideoItem(p);
      const thumbSrc = inQuarantine || p.in_quarantine
        ? `/api/quarantine/${p.id}/thumb`
        : `/api/photos/${p.id}/thumb`;
      const card = document.createElement("div");
      card.className =
        "card" +
        (state.selected.has(p.id) ? " selected" : "") +
        (isVideo ? " video-placeholder" : "");
      card.innerHTML = `
        <input type="checkbox" class="check" data-id="${p.id}" ${state.selected.has(p.id) ? "checked" : ""} />
        ${isVideo ? `<span class="play-badge" title="Video"></span>` : ""}
        <img loading="lazy" src="${thumbSrc}" alt=""
          onerror="this.style.background='#1e2a38';this.removeAttribute('src')" />
        <div class="cap">${escapeHtml(p.filename)}</div>
      `;
      const check = card.querySelector(".check");
      check.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = Number(check.dataset.id);
        if (check.checked) state.selected.add(id);
        else state.selected.delete(id);
        card.classList.toggle("selected", check.checked);
        updateSelectionCount();
      });
      card.addEventListener("click", (e) => {
        if (e.target.closest(".check")) return;
        if (state.smartFolder === "quarantine") {
          // Toggle selection on card click in quarantine for clearer UX
          check.checked = !check.checked;
          const id = Number(check.dataset.id);
          if (check.checked) state.selected.add(id);
          else state.selected.delete(id);
          card.classList.toggle("selected", check.checked);
          updateSelectionCount();
          return;
        }
        openLightbox(p);
      });
      card.addEventListener("dblclick", (e) => {
        e.preventDefault();
        openLightbox(p);
      });
      grid.appendChild(card);
    }
    updateSelectionCount();
  } catch (e) {
    grid.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

$("folder-filter").addEventListener("change", () => {
  state.smartFolder = "";
  state.nameBrowse = "";
  state.personId = null;
  if ($("smart-filter")) $("smart-filter").value = "";
  if ($("search")) $("search").value = "";
  state.offset = 0;
  renderPeopleBrowse();
  renderNameBrowse();
  loadPhotos();
});
$("month-filter").addEventListener("change", () => {
  state.offset = 0;
  loadPhotos();
});
$("year-filter")?.addEventListener("change", () => {
  state.offset = 0;
  // Clear month when picking year alone so year filter applies
  if ($("year-filter").value && $("month-filter")) {
    /* keep month if it matches year; else clear */
    const m = $("month-filter").value;
    if (m && !m.startsWith($("year-filter").value)) $("month-filter").value = "";
  }
  loadPhotos();
});
$("sort-filter")?.addEventListener("change", () => {
  state.sort = $("sort-filter").value || "date_desc";
  state.offset = 0;
  loadPhotos();
});
$("smart-filter")?.addEventListener("change", () => {
  setSmartFolder($("smart-filter").value);
});
let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.smartFolder = "";
    state.personId = null;
    state.nameBrowse = ($("search").value || "").trim();
    if ($("smart-filter")) $("smart-filter").value = "";
    state.offset = 0;
    renderPeopleBrowse();
    renderNameBrowse();
    loadPhotos();
  }, 300);
});
let nameBrowseTimer;
$("name-browse-filter")?.addEventListener("input", () => {
  clearTimeout(nameBrowseTimer);
  nameBrowseTimer = setTimeout(() => renderNameBrowse(), 150);
});
let peopleBrowseTimer;
$("people-browse-filter")?.addEventListener("input", () => {
  clearTimeout(peopleBrowseTimer);
  peopleBrowseTimer = setTimeout(() => renderPeopleBrowse(), 150);
});
$("btn-prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadPhotos();
});
$("btn-next").addEventListener("click", () => {
  state.offset += state.limit;
  loadPhotos();
});

function isVideoItem(p) {
  if ((p.media_type || "") === "video") return true;
  const name = (p.filename || p.rel_path || "").toLowerCase();
  return /\.(mp4|mov|m4v|avi|mkv|webm|3gp)$/i.test(name);
}

function formatDuration(sec) {
  if (sec == null || !Number.isFinite(Number(sec))) return "";
  const s = Math.max(0, Math.round(Number(sec)));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}:${String(r).padStart(2, "0")}` : `0:${String(r).padStart(2, "0")}`;
}

function mediaFileUrl(p) {
  const inQ =
    p.in_quarantine || state.smartFolder === "quarantine" || dupeState.view === "quarantine";
  return inQ ? `/api/quarantine/${p.id}/file` : `/api/photos/${p.id}/file`;
}

function closeLightbox() {
  $("lightbox").classList.add("hidden");
  const vid = $("lightbox-video");
  if (vid) {
    vid.pause();
    vid.removeAttribute("src");
    vid.load();
    vid.classList.add("hidden");
    vid.muted = false;
  }
  const img = $("lightbox-img");
  if (img) {
    img.removeAttribute("src");
    img.classList.remove("hidden");
  }
}

function openLightbox(p) {
  $("lightbox").classList.remove("hidden");
  const img = $("lightbox-img");
  const vid = $("lightbox-video");
  const isVideo = isVideoItem(p);
  const fileUrl = mediaFileUrl(p);
  if (isVideo && vid) {
    img.classList.add("hidden");
    img.removeAttribute("src");
    vid.classList.remove("hidden");
    vid.controls = true;
    vid.playsInline = true;
    vid.src = fileUrl;
    vid.currentTime = 0;
    const play = () => {
      const attempt = vid.play();
      if (attempt && typeof attempt.catch === "function") {
        attempt.catch(() => {
          /* autoplay blocked — controls still work */
        });
      }
    };
    vid.addEventListener("loadeddata", play, { once: true });
  } else {
    if (vid) {
      vid.pause();
      vid.removeAttribute("src");
      vid.classList.add("hidden");
    }
    img.classList.remove("hidden");
    img.src = fileUrl;
  }
  const dims =
    p.width || p.height ? `${p.width || "?"}×${p.height || "?"}` : isVideo ? "video" : "?×?";
  const dur = formatDuration(p.duration);
  const where = p.in_quarantine || state.smartFolder === "quarantine" || dupeState.view === "quarantine"
    ? `${p.original_rel_path || p.rel_path || p.filename} · quarantined`
    : `${p.rel_path} · ${dims}${dur ? ` · ${dur}` : ""} · ${p.exif_date || "no date"}`;
  $("lightbox-meta").textContent = where;
}
$("lightbox-close").addEventListener("click", closeLightbox);
$("lightbox").addEventListener("click", (e) => {
  if (e.target.id === "lightbox") closeLightbox();
});

/* Map */
async function loadMap() {
  const el = $("map");
  if (!el || typeof L === "undefined") return;

  if (!state.map) {
    state.map = L.map(el, { worldCopyJump: true }).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    state.mapMarkers = L.layerGroup().addTo(state.map);
  }

  // Leaflet needs a invalidate after becoming visible
  setTimeout(() => state.map.invalidateSize(), 50);

  try {
    const data = await api("/api/map/points?limit=5000");
    const points = data.points || [];
    state.mapMarkers.clearLayers();
    const bounds = [];
    for (const pt of points) {
      if (pt.lat == null || pt.lon == null) continue;
      const marker = L.marker([pt.lat, pt.lon]);
      marker.bindTooltip(pt.rel_path || String(pt.id), { direction: "top" });
      marker.on("click", async () => {
        try {
          const photo = await api(`/api/photos/${pt.id}`);
          openLightbox(photo);
        } catch (e) {
          openLightbox({
            id: pt.id,
            rel_path: pt.rel_path,
            media_type: "image",
            exif_date: pt.exif_date,
          });
        }
      });
      marker.addTo(state.mapMarkers);
      bounds.push([pt.lat, pt.lon]);
    }
    const countEl = $("map-count");
    if (countEl) {
      countEl.textContent = points.length
        ? `${points.length.toLocaleString()} located`
        : "No GPS photos yet";
    }
    if (bounds.length) {
      state.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
    }
    state.mapLoaded = true;
  } catch (e) {
    const countEl = $("map-count");
    if (countEl) countEl.textContent = e.message;
  }
}

$("btn-map-refresh")?.addEventListener("click", () => loadMap());

/* Duplicates */
const dupeState = {
  kind: "exact",
  view: "dupes", // dupes | quarantine
  offset: 0,
  limit: 40,
  total: 0,
  loading: false,
};

function startInlineVideo(card, p) {
  const media = card.querySelector(".dupe-media");
  if (!media) return;
  let vid = media.querySelector("video");
  if (vid) {
    if (vid.paused) {
      vid.play().catch(() => {});
    } else {
      vid.pause();
    }
    return;
  }
  const img = media.querySelector("img");
  const badge = media.querySelector(".play-badge");
  if (img) img.hidden = true;
  if (badge) badge.hidden = true;
  vid = document.createElement("video");
  vid.controls = true;
  vid.playsInline = true;
  vid.preload = "metadata";
  vid.src = mediaFileUrl(p);
  media.appendChild(vid);
  const playBtn = card.querySelector(".play-inline-btn");
  if (playBtn) playBtn.textContent = "Pause / play";
  vid.addEventListener("loadeddata", () => {
    vid.play().catch(() => {});
  }, { once: true });
}

function showDupeFlash(message, kind = "ok") {
  const flash = $("dupe-flash");
  if (!flash) return;
  flash.hidden = false;
  flash.className = `dupe-flash ${kind}`;
  flash.textContent = message;
}

function renderDupeGroup(g) {
  const el = document.createElement("div");
  el.className = "dupe-group";
  el.dataset.groupKey = g.key;
  const keepDefault = g.photos[0]?.id;
  el.innerHTML = `
    <h3>
      <span class="dupe-group-title">
        <label class="inline-check dupe-group-pick">
          <input type="checkbox" class="dupe-group-check" />
          Select
        </label>
        <span class="badge ${g.type}">${g.type}</span> ${g.count} files
      </span>
      <span class="dupe-group-actions">
        <button class="btn btn-sm btn-danger resolve-btn">Keep selected &amp; remove others</button>
        <button class="btn btn-sm btn-ghost resolve-all-btn" type="button">Remove all</button>
      </span>
    </h3>
    <div class="dupe-grid"></div>
  `;
  const grid = el.querySelector(".dupe-grid");
  const cardById = new Map();
  for (const p of g.photos) {
    const card = document.createElement("div");
    const isVideo = isVideoItem(p);
    card.className = "dupe-card" + (isVideo ? " is-video" : "");
    card.dataset.id = String(p.id);
    const dur = formatDuration(p.duration);
    card.innerHTML = `
      <div class="dupe-media">
        <img src="/api/photos/${p.id}/thumb" alt="" loading="lazy" />
        ${isVideo ? `<span class="play-badge" title="Video"></span>` : ""}
      </div>
      <div class="meta">${escapeHtml(p.rel_path)}<br/>${formatBytes(p.size || 0)}${dur ? ` · ${dur}` : ""}${isVideo ? " · video" : ""}</div>
      <div class="dupe-card-actions">
        ${isVideo ? `<button type="button" class="btn btn-ghost btn-sm play-inline-btn">Play here</button>` : ""}
        <button type="button" class="btn btn-ghost btn-sm preview-btn">${isVideo ? "Full screen" : "Preview"}</button>
      </div>
      <label class="inline-check"><input type="radio" name="keep-${g.key}" value="${p.id}" ${p.id === keepDefault ? "checked" : ""}/> Keep</label>
    `;
    const media = card.querySelector(".dupe-media");
    const img = card.querySelector("img");
    card.querySelector(".preview-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      openLightbox(p);
    });
    img.addEventListener("click", () => {
      if (isVideo) startInlineVideo(card, p);
      else openLightbox(p);
    });
    const playBtn = card.querySelector(".play-inline-btn");
    if (playBtn) {
      playBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        startInlineVideo(card, p);
      });
    }
    grid.appendChild(card);
    cardById.set(p.id, card);
  }
  el._dupeGroup = g;
  el._cardById = cardById;
  const btn = el.querySelector(".resolve-btn");
  btn.addEventListener("click", async () => {
    const mode = $("dupe-delete").checked ? "delete" : "quarantine";
    const result = await resolveDupeGroupEl(el, { confirm: true });
    if (result.ok) {
      const where =
        mode === "quarantine"
          ? ` Moved to ${result.quarantine_dir || "_quarantine_duplicates"} on your drive.`
          : " Permanently deleted from the drive.";
      showDupeFlash(`Removed ${result.removed} duplicate(s).${where}`, "ok");
      const meta = $("dupe-meta");
      if (meta && !meta.hidden) {
        meta.textContent = meta.textContent.replace(
          /^\d[\d,]* /,
          `${dupeState.total.toLocaleString()} `
        );
      }
      refreshStats();
    } else if (result.error) {
      showDupeFlash(`Failed: ${result.error}`, "err");
    } else if (result.errors) {
      showDupeFlash(`Done with errors: ${JSON.stringify(result.errors)}`, "warn");
      refreshStats();
    }
  });
  el.querySelector(".resolve-all-btn").addEventListener("click", async () => {
    const mode = $("dupe-delete").checked ? "delete" : "quarantine";
    const result = await resolveDupeGroupEl(el, { confirm: true, deleteAll: true });
    if (result.ok) {
      const where =
        mode === "quarantine"
          ? ` All moved to ${result.quarantine_dir || "_quarantine_duplicates"}.`
          : " All permanently deleted.";
      showDupeFlash(`Removed all ${result.removed} file(s) in group.${where}`, "ok");
      const meta = $("dupe-meta");
      if (meta && !meta.hidden) {
        meta.textContent = meta.textContent.replace(
          /^\d[\d,]* /,
          `${dupeState.total.toLocaleString()} `
        );
      }
      refreshStats();
      loadFilters();
    } else if (result.error) {
      showDupeFlash(`Failed: ${result.error}`, "err");
    } else if (result.errors) {
      showDupeFlash(`Done with errors: ${JSON.stringify(result.errors)}`, "warn");
      refreshStats();
    }
  });
  return el;
}

async function resolveDupeGroupEl(el, { confirm: doConfirm = true, deleteAll = false } = {}) {
  const g = el._dupeGroup;
  const cardById = el._cardById;
  const btn = el.querySelector(deleteAll ? ".resolve-all-btn" : ".resolve-btn");
  const otherBtn = el.querySelector(deleteAll ? ".resolve-btn" : ".resolve-all-btn");
  if (!g || el.classList.contains("is-resolving") || el.classList.contains("is-resolved")) {
    return { ok: false, skipped: true };
  }

  let keep = null;
  let deleteIds;
  if (deleteAll) {
    deleteIds = g.photos.map((p) => p.id);
  } else {
    const checked = el.querySelector(`input[name="keep-${g.key}"]:checked`);
    if (!checked) {
      showDupeFlash("Select which photo to keep first.", "warn");
      return { ok: false };
    }
    keep = Number(checked.value);
    deleteIds = g.photos.map((p) => p.id).filter((id) => id !== keep);
  }
  if (!deleteIds.length) {
    showDupeFlash("Nothing to remove in this group.", "warn");
    return { ok: false };
  }
  const mode = $("dupe-delete").checked ? "delete" : "quarantine";
  if (doConfirm) {
    let msg;
    if (deleteAll) {
      msg =
        mode === "delete"
          ? `Permanently delete ALL ${deleteIds.length} file(s) in this group? None will be kept.`
          : `Move ALL ${deleteIds.length} file(s) in this group to quarantine? None will stay in the library.`;
    } else {
      const actionLabel =
        mode === "delete"
          ? `permanently delete ${deleteIds.length} duplicate(s)`
          : `move ${deleteIds.length} duplicate(s) to _quarantine_duplicates`;
      msg = `Keep one photo and ${actionLabel}?`;
    }
    if (!confirm(msg)) {
      return { ok: false, cancelled: true };
    }
  }

  const prevLabel = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = mode === "delete" ? "Deleting…" : "Quarantining…";
  }
  if (otherBtn) otherBtn.disabled = true;
  el.classList.add("is-resolving");

  try {
    const result = await api("/api/duplicates/resolve", {
      method: "POST",
      body: JSON.stringify({
        keep_id: deleteAll ? null : keep,
        delete_ids: deleteIds,
        mode,
        delete_all: deleteAll,
      }),
    });
    const removed = new Set(result.removed || []);
    for (const id of removed) {
      const card = cardById.get(id);
      if (card) card.classList.add("is-removing");
    }

    const n = result.removed_count ?? removed.size;
    const errN = result.error_count ?? (result.errors?.length || 0);
    if (n > 0 && errN === 0) {
      el.classList.add("is-resolved");
      setTimeout(() => el.remove(), 380);
      dupeState.total = Math.max(0, (dupeState.total || 1) - 1);
      return {
        ok: true,
        removed: n,
        mode,
        quarantine_dir: result.quarantine_dir,
        deleteAll,
      };
    }
    if (n > 0) {
      for (const id of removed) cardById.get(id)?.remove();
      el.classList.remove("is-resolving");
      if (btn) {
        btn.disabled = false;
        btn.textContent = prevLabel;
      }
      if (otherBtn) otherBtn.disabled = false;
      return { ok: false, removed: n, errors: result.errors };
    }
    el.classList.remove("is-resolving");
    if (btn) {
      btn.disabled = false;
      btn.textContent = prevLabel;
    }
    if (otherBtn) otherBtn.disabled = false;
    return { ok: false, errors: result.errors };
  } catch (e) {
    el.classList.remove("is-resolving");
    if (btn) {
      btn.disabled = false;
      btn.textContent = prevLabel;
    }
    if (otherBtn) otherBtn.disabled = false;
    return { ok: false, error: e.message };
  }
}

function setDupeBulkBarVisible(show) {
  const bar = $("dupe-bulk-bar");
  if (bar) bar.hidden = !show;
}

function selectAllDupeGroups(selected) {
  const box = $("dupe-results");
  if (!box) return;
  for (const check of box.querySelectorAll(".dupe-group-check")) {
    check.checked = selected;
  }
}

async function resolveSelectedDupeGroups({ deleteAll = false } = {}) {
  const box = $("dupe-results");
  if (!box) return;
  const groups = [...box.querySelectorAll(".dupe-group")].filter((el) => {
    const check = el.querySelector(".dupe-group-check");
    return check && check.checked && !el.classList.contains("is-resolved");
  });
  if (!groups.length) {
    showDupeFlash("Select one or more groups first (checkbox on each group).", "warn");
    return;
  }
  const mode = $("dupe-delete").checked ? "delete" : "quarantine";
  let confirmMsg;
  if (deleteAll) {
    confirmMsg =
      mode === "delete"
        ? `Permanently delete ALL files in ${groups.length} group(s)? Nothing will be kept.`
        : `Move ALL files in ${groups.length} group(s) to quarantine? Nothing stays in the library.`;
  } else {
    const verb = mode === "delete" ? "permanently delete extras in" : "quarantine extras in";
    confirmMsg = `Resolve ${groups.length} group(s)? This will ${verb} each selected group, keeping the photo marked Keep.`;
  }
  if (!confirm(confirmMsg)) return;

  const resolveBtn = $(deleteAll ? "btn-dupe-remove-all-selected" : "btn-dupe-resolve-selected");
  const otherBtn = $(deleteAll ? "btn-dupe-resolve-selected" : "btn-dupe-remove-all-selected");
  const idleLabel = deleteAll ? "Remove all in selected" : "Resolve selected";
  if (resolveBtn) {
    resolveBtn.disabled = true;
    resolveBtn.textContent = `${deleteAll ? "Removing" : "Resolving"} 0/${groups.length}…`;
  }
  if (otherBtn) otherBtn.disabled = true;
  let ok = 0;
  let failed = 0;
  let removedTotal = 0;
  for (let i = 0; i < groups.length; i++) {
    if (resolveBtn) {
      resolveBtn.textContent = `${deleteAll ? "Removing" : "Resolving"} ${i + 1}/${groups.length}…`;
    }
    showDupeFlash(
      `${deleteAll ? "Removing all in" : "Resolving"} group ${i + 1} of ${groups.length} (${mode}) — wait…`,
      "warn"
    );
    const result = await resolveDupeGroupEl(groups[i], { confirm: false, deleteAll });
    if (result.ok) {
      ok += 1;
      removedTotal += result.removed || 0;
    } else if (!result.cancelled && !result.skipped) {
      failed += 1;
    }
  }
  refreshStats();
  loadFilters();
  const meta = $("dupe-meta");
  if (meta && !meta.hidden) {
    meta.textContent = meta.textContent.replace(
      /^\d[\d,]* /,
      `${dupeState.total.toLocaleString()} `
    );
  }
  if (resolveBtn) {
    resolveBtn.disabled = false;
    resolveBtn.textContent = idleLabel;
  }
  if (otherBtn) otherBtn.disabled = false;
  showDupeFlash(
    `Bulk done: ${ok} group(s), ${removedTotal} file(s) removed` +
      (failed ? `, ${failed} failed` : "") +
      (mode === "quarantine" ? ". Check Quarantine to review." : "."),
    failed ? "warn" : "ok"
  );
}

async function showDupes(kind, { append = false } = {}) {
  const box = $("dupe-results");
  const meta = $("dupe-meta");
  const pager = $("dupe-pager");
  if (dupeState.loading) return;
  dupeState.loading = true;
  dupeState.kind = kind;
  dupeState.view = "dupes";
  setDupeBulkBarVisible(true);
  if (!append) {
    dupeState.offset = 0;
    box.innerHTML = `<div class="empty">${kind === "near" ? "Searching near-duplicates (large libraries can take a minute)…" : "Loading exact duplicates…"}</div>`;
  } else {
    const more = $("btn-dupe-more");
    if (more) more.disabled = true;
  }
  try {
    const params = new URLSearchParams({
      limit: String(dupeState.limit),
      offset: String(dupeState.offset),
    });
    const data = await api(
      kind === "exact" ? `/api/duplicates/exact?${params}` : `/api/duplicates/near?${params}`
    );
    dupeState.total = data.total_groups || 0;
    if (!append) box.innerHTML = "";
    if (!data.groups.length && !append) {
      box.innerHTML = `<div class="empty">No ${kind} duplicates found.</div>`;
      if (meta) meta.hidden = true;
      if (pager) pager.hidden = true;
      setDupeBulkBarVisible(false);
      return;
    }
    for (const g of data.groups) {
      box.appendChild(renderDupeGroup(g));
    }
    dupeState.offset += data.groups.length;
    const shown = Math.min(dupeState.offset, dupeState.total);
    if (meta) {
      meta.hidden = false;
      const extra =
        kind === "near" && data.compared
          ? ` · compared ${Number(data.compared).toLocaleString()} unique hashes`
          : "";
      meta.textContent = `${dupeState.total.toLocaleString()} ${kind} groups · showing ${shown.toLocaleString()}${extra}`;
    }
    if (pager) {
      pager.hidden = !data.truncated;
      const more = $("btn-dupe-more");
      if (more) more.disabled = false;
    }
  } catch (e) {
    if (!append) {
      box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    } else {
      alert(e.message);
    }
  } finally {
    dupeState.loading = false;
    const more = $("btn-dupe-more");
    if (more) more.disabled = false;
  }
}

$("btn-exact").addEventListener("click", () => {
  dupeState.view = "dupes";
  showDupes("exact");
});
$("btn-near").addEventListener("click", () => {
  dupeState.view = "dupes";
  showDupes("near");
});
$("btn-dupe-more")?.addEventListener("click", () => {
  if (dupeState.view === "quarantine") showQuarantineDupes({ append: true });
  else showDupes(dupeState.kind, { append: true });
});

async function showQuarantineDupes({ append = false } = {}) {
  const box = $("dupe-results");
  const meta = $("dupe-meta");
  const pager = $("dupe-pager");
  if (dupeState.loading) return;
  dupeState.loading = true;
  dupeState.view = "quarantine";
  setDupeBulkBarVisible(false);
  if (!append) {
    dupeState.offset = 0;
    box.innerHTML = `<div class="empty">Loading quarantine…</div>`;
  }
  try {
    const params = new URLSearchParams({
      limit: String(dupeState.limit),
      offset: String(dupeState.offset),
    });
    const data = await api(`/api/quarantine?${params}`);
    dupeState.total = data.total || 0;

    let grid = box.querySelector("#quarantine-dupe-grid");
    if (!append || !grid) {
      box.innerHTML = "";
      if (!data.items.length && dupeState.offset === 0) {
        box.innerHTML = `<div class="empty">Quarantine is empty.</div>`;
        if (meta) {
          meta.hidden = false;
          meta.textContent = data.quarantine_dir
            ? `Folder: ${data.quarantine_dir}`
            : "No quarantined files";
        }
        if (pager) pager.hidden = true;
        return;
      }
      const el = document.createElement("div");
      el.className = "dupe-group";
      el.innerHTML = `
        <h3>
          <span class="dupe-group-title">
            <span class="badge near">quarantine</span> select items to delete forever
            <span id="q-dupe-sel-count" class="sel-pill">0 selected</span>
          </span>
          <span class="dupe-group-actions">
            <button class="btn btn-sm btn-ghost" type="button" id="btn-q-dupe-select-all">Select all</button>
            <button class="btn btn-sm btn-ghost" type="button" id="btn-q-dupe-clear">Clear</button>
            <button class="btn btn-sm btn-danger" type="button" id="btn-q-dupe-delete">Delete selected forever</button>
          </span>
        </h3>
        <div class="dupe-grid" id="quarantine-dupe-grid"></div>
      `;
      box.appendChild(el);
      grid = el.querySelector("#quarantine-dupe-grid");

      const syncQDupeSelection = () => {
        const checks = [...box.querySelectorAll(".q-check")];
        let n = 0;
        for (const c of checks) {
          const card = c.closest(".dupe-card");
          if (card) card.classList.toggle("is-q-selected", c.checked);
          if (c.checked) n += 1;
        }
        const pill = $("q-dupe-sel-count");
        if (pill) {
          pill.textContent = `${n.toLocaleString()} selected`;
          pill.classList.toggle("has-selection", n > 0);
        }
        const allBtn = $("btn-q-dupe-select-all");
        if (allBtn) {
          allBtn.textContent =
            checks.length && checks.every((c) => c.checked) ? "All selected" : "Select all";
        }
      };

      el.querySelector("#btn-q-dupe-select-all").addEventListener("click", () => {
        for (const c of box.querySelectorAll(".q-check")) c.checked = true;
        syncQDupeSelection();
      });
      el.querySelector("#btn-q-dupe-clear").addEventListener("click", () => {
        for (const c of box.querySelectorAll(".q-check")) c.checked = false;
        syncQDupeSelection();
      });
      el._syncQDupeSelection = syncQDupeSelection;
      el.querySelector("#btn-q-dupe-delete").addEventListener("click", async () => {
        const ids = [...box.querySelectorAll(".q-check:checked")].map((c) => Number(c.value));
        if (!ids.length) {
          showDupeFlash("Select at least one quarantined file.", "warn");
          return;
        }
        const token = promptConfirmToken(
          CONFIRM_DELETE_FOREVER,
          `Permanently delete ${ids.length} quarantined file(s)? This cannot be undone.`
        );
        if (!token) return;
        const delBtn = el.querySelector("#btn-q-dupe-delete");
        delBtn.disabled = true;
        delBtn.textContent = "Deleting…";
        showDupeFlash(`Deleting ${ids.length} file(s)…`, "warn");
        try {
          const result = await api("/api/quarantine/delete", {
            method: "POST",
            body: JSON.stringify({ ids, confirm_token: token }),
          });
          showDupeFlash(
            `Deleted ${result.deleted_count || 0} file(s) forever.` +
              (result.errors?.length ? ` Errors: ${JSON.stringify(result.errors)}` : ""),
            result.errors?.length ? "warn" : "ok"
          );
          showQuarantineDupes();
          loadFilters();
          refreshStats();
        } catch (e) {
          showDupeFlash(`Failed: ${e.message}`, "err");
          delBtn.disabled = false;
          delBtn.textContent = "Delete selected forever";
        }
      });
    }

    for (const p of data.items) {
      const card = document.createElement("div");
      card.className = "dupe-card";
      card.dataset.id = String(p.id);
      card.innerHTML = `
        <img src="/api/quarantine/${p.id}/thumb" alt="" loading="lazy" />
        <div class="meta">${escapeHtml(p.original_rel_path || p.filename)}<br/>${formatBytes(p.size || 0)}</div>
        <label class="inline-check"><input type="checkbox" class="q-check" value="${p.id}"/> Select</label>
      `;
      card.querySelector("img").addEventListener("click", () =>
        openLightbox({ ...p, in_quarantine: true })
      );
      const check = card.querySelector(".q-check");
      check.addEventListener("change", () => {
        const fn = box.querySelector(".dupe-group")?._syncQDupeSelection;
        if (fn) fn();
      });
      card.addEventListener("click", (e) => {
        if (e.target.closest("img") || e.target.closest("a")) return;
        if (e.target.closest(".q-check") || e.target.closest("label")) return;
        check.checked = !check.checked;
        const fn = box.querySelector(".dupe-group")?._syncQDupeSelection;
        if (fn) fn();
      });
      grid.appendChild(card);
    }
    const fn = box.querySelector(".dupe-group")?._syncQDupeSelection;
    if (fn) fn();

    dupeState.offset += data.items.length;
    const shown = Math.min(dupeState.offset, dupeState.total);
    if (meta) {
      meta.hidden = false;
      meta.textContent =
        `${dupeState.total.toLocaleString()} in quarantine · showing ${shown.toLocaleString()}` +
        (data.quarantine_dir ? ` · ${data.quarantine_dir}` : "");
    }
    if (pager) pager.hidden = dupeState.offset >= dupeState.total;
  } catch (e) {
    if (!append) box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    else alert(e.message);
  } finally {
    dupeState.loading = false;
  }
}

$("btn-dupe-quarantine")?.addEventListener("click", () => showQuarantineDupes());
$("btn-dupe-select-all")?.addEventListener("click", () => selectAllDupeGroups(true));
$("btn-dupe-clear")?.addEventListener("click", () => selectAllDupeGroups(false));
$("btn-dupe-resolve-selected")?.addEventListener("click", () => resolveSelectedDupeGroups());
$("btn-dupe-remove-all-selected")?.addEventListener("click", () =>
  resolveSelectedDupeGroups({ deleteAll: true })
);

/* Full exact dedupe batch */
let dupeBatchPoll = null;

function formatBytesApprox(n) {
  return formatBytes(n || 0);
}

async function loadExactBatchPreview() {
  const el = $("dupe-batch-preview");
  if (!el) return;
  try {
    const p = await api("/api/duplicates/exact/batch/preview");
    if (!p.groups) {
      el.textContent = "No exact duplicate groups found — nothing to batch-remove.";
      return;
    }
    el.textContent =
      `${p.groups.toLocaleString()} groups · keep ${p.files_to_keep.toLocaleString()} · ` +
      `permanently delete ${p.files_to_remove.toLocaleString()} extras` +
      ` (~${formatBytesApprox(p.approx_bytes_to_remove)})`;
  } catch (e) {
    el.textContent = e.message;
  }
}

function renderExactBatchStatus(s) {
  const statusEl = $("dupe-batch-status");
  const bar = $("dupe-batch-bar");
  const fill = $("dupe-batch-bar-fill");
  const cancelBtn = $("btn-dupe-batch-cancel");
  const startBtn = $("btn-dupe-batch-start");
  if (!statusEl) return;

  const running = s.status === "running";
  statusEl.hidden = s.status === "idle";
  updateJobProgress(bar, fill, {
    processed: s.groups_done,
    total: s.groups_total,
    visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
    indeterminate: running && !(s.groups_total > 0),
  });
  if (s.percent != null && fill && s.groups_total > 0) {
    fill.style.width = `${s.percent}%`;
  }
  if (cancelBtn) cancelBtn.hidden = !running;
  if (startBtn) startBtn.disabled = running;

  if (s.status === "idle") {
    statusEl.textContent = "";
    return;
  }
  const bits = [
    s.status,
    s.message || "",
    s.groups_done != null ? `${s.groups_done}/${s.groups_total} groups` : "",
    s.files_removed != null ? `${s.files_removed.toLocaleString()} removed` : "",
    s.elapsed_seconds != null ? `${formatElapsed(s.elapsed_seconds)} elapsed` : "",
  ].filter(Boolean);
  statusEl.textContent = bits.join(" · ");
}

async function pollExactBatch() {
  try {
    const s = await api("/api/duplicates/exact/batch/status");
    renderExactBatchStatus(s);
    if (s.status === "running") {
      if (!dupeBatchPoll) dupeBatchPoll = setInterval(pollExactBatch, 1500);
    } else if (dupeBatchPoll) {
      clearInterval(dupeBatchPoll);
      dupeBatchPoll = null;
      if (s.status === "completed" || s.status === "cancelled") {
        loadExactBatchPreview();
        refreshStats();
        loadFilters();
      }
    }
  } catch {
    /* ignore */
  }
}

$("btn-dupe-batch-refresh")?.addEventListener("click", () => loadExactBatchPreview());
$("btn-dupe-batch-start")?.addEventListener("click", async () => {
  if (!$("dupe-batch-confirm")?.checked) {
    alert("Check the confirmation box first — this permanently deletes files.");
    return;
  }
  const preview = $("dupe-batch-preview")?.textContent || "";
  if (
    !confirm(
      `Run full exact dedupe with PERMANENT DELETE?\n\n${preview}\n\nThis cannot be undone.`
    )
  ) {
    return;
  }
  try {
    const s = await api("/api/duplicates/exact/batch/start", {
      method: "POST",
      body: JSON.stringify({ mode: "delete", confirm: true }),
    });
    renderExactBatchStatus(s);
    pollExactBatch();
  } catch (e) {
    alert(e.message);
  }
});
$("btn-dupe-batch-cancel")?.addEventListener("click", async () => {
  try {
    const s = await api("/api/duplicates/exact/batch/cancel", {
      method: "POST",
      body: "{}",
    });
    renderExactBatchStatus(s);
  } catch (e) {
    alert(e.message);
  }
});

document.querySelectorAll('.nav-item[data-view="duplicates"]').forEach((btn) => {
  btn.addEventListener("click", () => {
    loadExactBatchPreview();
    pollExactBatch();
  });
});

$("btn-q-select-all")?.addEventListener("click", () => selectAllBrowseVisible(true));
$("btn-q-clear")?.addEventListener("click", () => {
  state.selected.clear();
  selectAllBrowseVisible(false);
});

$("btn-browse-select-all")?.addEventListener("click", () => selectAllBrowseVisible(true));
$("btn-browse-clear")?.addEventListener("click", () => {
  state.selected.clear();
  selectAllBrowseVisible(false);
});

async function removeSelectedLibraryPhotos(mode) {
  if (state.smartFolder === "quarantine") {
    alert("Use the Quarantine bar for quarantined items.");
    return;
  }
  const ids = [...state.selected];
  if (!ids.length) {
    alert("Select one or more photos in Browse first.");
    return;
  }
  let confirmToken = null;
  if (mode === "delete") {
    confirmToken = promptConfirmToken(
      CONFIRM_DELETE_FOREVER,
      `Permanently delete ${ids.length} photo(s) from your drive? This cannot be undone.`
    );
    if (!confirmToken) return;
  } else if (
    !confirm(
      `Move ${ids.length} photo(s) to quarantine (_quarantine_duplicates)?\n\n` +
        "They leave the library but stay on the drive until you Delete forever from Quarantine."
    )
  ) {
    return;
  }

  const chunkSize = 500;
  let removed = 0;
  let errors = [];
  try {
    for (let i = 0; i < ids.length; i += chunkSize) {
      const chunk = ids.slice(i, i + chunkSize);
      const payload = { photo_ids: chunk, mode };
      if (confirmToken) payload.confirm_token = confirmToken;
      const result = await api("/api/photos/remove", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      removed += result.removed_count || 0;
      if (result.errors?.length) errors = errors.concat(result.errors);
    }
    const sample = errors
      .slice(0, 5)
      .map((e) => `#${e.id}: ${e.error}`)
      .join("\n");
    alert(
      (mode === "delete"
        ? `Deleted ${removed} forever.`
        : `Moved ${removed} to quarantine.`) +
        (errors.length ? ` ${errors.length} error(s).` : "") +
        (sample ? `\n\n${sample}${errors.length > 5 ? "\n…" : ""}` : "")
    );
    state.selected.clear();
    updateSelectionCount();
    loadFilters();
    loadPhotos();
    refreshStats();
  } catch (e) {
    alert(e.message);
  }
}

$("btn-browse-quarantine")?.addEventListener("click", () =>
  removeSelectedLibraryPhotos("quarantine")
);
$("btn-browse-delete")?.addEventListener("click", () =>
  removeSelectedLibraryPhotos("delete")
);

async function quarantineSelection(action) {
  if (state.smartFolder !== "quarantine") {
    alert(
      "Delete forever / Restore only work in the Quarantine smart folder. " +
        "Library photos (including Screenshots) are not deleted from here."
    );
    return;
  }
  const ids = [...state.selected];
  if (!ids.length) {
    alert("Select one or more quarantined items in Browse first.");
    return;
  }
  let confirmToken = null;
  if (action === "delete") {
    confirmToken = promptConfirmToken(
      CONFIRM_DELETE_FOREVER,
      `Permanently delete ${ids.length} quarantined file(s)? This cannot be undone.`
    );
    if (!confirmToken) return;
  } else if (
    !confirm(
      `Restore ${ids.length} file(s) out of quarantine? They go back to their original folder when known, otherwise Inbox. Run Resume scan afterward to re-index.`
    )
  ) {
    return;
  }
  try {
    const payload = { ids };
    if (confirmToken) payload.confirm_token = confirmToken;
    const result = await api(`/api/quarantine/${action}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const n =
      action === "delete" ? result.deleted_count || 0 : result.restored_count || 0;
    const errList = result.errors || [];
    const err = errList.length;
    const sample = errList
      .slice(0, 5)
      .map((e) => `#${e.id}: ${e.error}`)
      .join("\n");
    alert(
      (action === "delete"
        ? `Deleted ${n} forever.${err ? ` ${err} error(s).` : ""}`
        : `Restored ${n}.${err ? ` ${err} error(s).` : ""} Run Resume scan to see them in the library.`) +
        (sample ? `\n\n${sample}${err > 5 ? "\n…" : ""}` : "")
    );
    state.selected.clear();
    updateSelectionCount();
    loadFilters();
    loadPhotos();
    refreshStats();
  } catch (e) {
    alert(e.message);
  }
}

$("btn-q-restore")?.addEventListener("click", () => quarantineSelection("restore"));
$("btn-q-delete")?.addEventListener("click", () => quarantineSelection("delete"));

/* Import */
async function pollImport() {
  try {
    const s = await api("/api/import/status");
    const running = s.status === "running";
    const cancelBtn = $("btn-import-cancel");
    if (cancelBtn) cancelBtn.hidden = !running;
    updateJobProgress($("import-bar"), $("import-bar-fill"), {
      processed: s.processed,
      total: s.total,
      visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
      indeterminate: running && !(s.total > 0),
    });
    const el = $("import-status");
    if (el) {
      el.textContent = [
        `${s.status}`,
        s.total
          ? `${s.processed || 0}/${s.total}`
          : null,
        `copied=${s.copied || 0}`,
        `exact_skip=${s.skipped_exact || 0}`,
        `near_skip=${s.skipped_near || 0}`,
        `errors=${s.errors || 0}`,
        s.message || null,
      ]
        .filter(Boolean)
        .join(" · ");
    }
    const report = $("import-report");
    if (report) {
      report.textContent = (s.report || [])
        .map((r) => JSON.stringify(r))
        .join("\n");
    }
    if (running) {
      if (!state.importPoll) state.importPoll = setInterval(pollImport, 1000);
      pollBusyJobs();
    } else if (state.importPoll) {
      clearInterval(state.importPoll);
      state.importPoll = null;
      pollBusyJobs();
      refreshStats();
    }
  } catch (e) {
    if ($("import-status")) $("import-status").textContent = e.message;
  }
}

$("btn-import-preview").addEventListener("click", async () => {
  try {
    const data = await api("/api/import/preview", {
      method: "POST",
      body: JSON.stringify({
        source_dir: $("import-source").value.trim(),
        check_near: $("import-near").checked,
      }),
    });
    $("import-status").textContent =
      `Preview: total=${data.total} would_copy=${data.would_copy} skip_exact=${data.skip_exact} skip_near=${data.skip_near} errors=${data.errors}`;
    $("import-report").textContent = JSON.stringify(
      {
        files_to_copy: data.files_to_copy?.slice(0, 50),
        skipped_exact: data.skipped_exact?.slice(0, 30),
        skipped_near: data.skipped_near?.slice(0, 30),
      },
      null,
      2
    );
  } catch (e) {
    $("import-status").textContent = e.message;
  }
});

$("btn-import-start").addEventListener("click", async () => {
  try {
    await api("/api/import/start", {
      method: "POST",
      body: JSON.stringify({
        source_dir: $("import-source").value.trim(),
        dest_folder: $("import-dest").value.trim() || "Inbox",
        check_near: $("import-near").checked,
      }),
    });
    pollImport();
    pollBusyJobs();
  } catch (e) {
    $("import-status").textContent = e.message;
  }
});

$("btn-import-cancel")?.addEventListener("click", async () => {
  await requestJobCancel("/api/import/cancel", "Import");
});

/* iCloud Archive */
function archiveFilterBody() {
  const mode = $("archive-mode")?.value || "keep_years";
  const body = {};
  if (mode === "selected") {
    body.asset_ids = [...state.archiveSelected];
  } else if (mode === "date_range") {
    body.range_start = $("archive-range-start").value || null;
    body.range_end = $("archive-range-end").value || null;
  } else {
    const val = Number($("archive-keep-value").value);
    const unit = $("archive-keep-unit").value;
    if (unit === "months") body.keep_months = val;
    else body.keep_years = val;
  }
  return body;
}

/** Keep window / date range only — used by Largest first (ignores selected mode). */
function archiveScopeFilterBody() {
  const mode = $("archive-mode")?.value || "keep_years";
  if (mode === "date_range") {
    return {
      range_start: $("archive-range-start").value || null,
      range_end: $("archive-range-end").value || null,
    };
  }
  const val = Number($("archive-keep-value").value);
  const unit = $("archive-keep-unit")?.value || "years";
  if (unit === "months") return { keep_months: val };
  return { keep_years: val };
}

function switchArchiveModeToSelected() {
  const mode = $("archive-mode");
  if (!mode || mode.value === "selected") return;
  mode.value = "selected";
  updateArchiveModeFields();
}

function rememberArchiveAssetSizes(assets) {
  for (const a of assets || []) {
    if (a?.asset_id == null) continue;
    state.archiveSizeById[a.asset_id] = Number(a.size) || 0;
    state.archiveTypeById[a.asset_id] = a.item_type === "video" ? "video" : "image";
  }
}

function selectedArchiveStats() {
  let bytes = 0;
  let photos = 0;
  let videos = 0;
  let photoBytes = 0;
  let videoBytes = 0;
  let unknown = 0;
  for (const id of state.archiveSelected) {
    const size = Number(state.archiveSizeById[id]);
    if (!Number.isFinite(size)) {
      unknown += 1;
      continue;
    }
    bytes += size;
    if (state.archiveTypeById[id] === "video") {
      videos += 1;
      videoBytes += size;
    } else {
      photos += 1;
      photoBytes += size;
    }
  }
  return {
    count: state.archiveSelected.size,
    bytes,
    photos,
    videos,
    photoBytes,
    videoBytes,
    unknown,
  };
}

function selectedArchiveBytes() {
  return selectedArchiveStats().bytes;
}

function updateArchiveSelCount() {
  const stats = selectedArchiveStats();
  const n = stats.count;
  const bytes = stats.bytes;
  const el = $("archive-sel-count");
  if (el) {
    el.textContent =
      n > 0
        ? `${n.toLocaleString()} selected · ${formatBytes(bytes)}`
        : "0 selected";
  }

  const summary = $("archive-selection-summary");
  const main = $("archive-sel-summary-main");
  const detail = $("archive-sel-summary-detail");
  if (summary) {
    summary.hidden = n === 0;
    summary.classList.toggle("is-large", bytes >= 5 * 1024 ** 3);
  }
  if (main && n > 0) {
    main.textContent = `${n.toLocaleString()} selected · ${formatBytes(bytes)}`;
  }
  if (detail && n > 0) {
    const bits = [];
    if (stats.videos) {
      bits.push(`${stats.videos.toLocaleString()} videos (${formatBytes(stats.videoBytes)})`);
    }
    if (stats.photos) {
      bits.push(`${stats.photos.toLocaleString()} photos (${formatBytes(stats.photoBytes)})`);
    }
    if (stats.unknown) bits.push(`${stats.unknown} size unknown`);
    bits.push("will transfer to WD if you Archive");
    detail.textContent = bits.join(" · ");
  }

  const footer = $("archive-largest-footer");
  if (footer) {
    if (n > 0) {
      footer.textContent =
        `${n.toLocaleString()} selected · ${formatBytes(bytes)} — ready for Estimate / Archive to WD.`;
    } else if (state.archiveLargestAssets.length) {
      footer.textContent = "Check rows to select, or Select all listed.";
    } else {
      footer.textContent = "";
    }
  }

  // Live-fill the estimate strip when working from a pick-list selection
  const mode = $("archive-mode")?.value;
  if (mode === "selected" && n > 0) {
    applyEstimate({
      count: n,
      bytes,
      photos: stats.photos,
      videos: stats.videos,
      photo_bytes: stats.photoBytes,
      video_bytes: stats.videoBytes,
      estimated_batches: Math.ceil(n / 500),
    });
    const bytesLabel = $("archive-est-bytes")?.parentElement?.querySelector("label");
    if (bytesLabel) bytesLabel.textContent = "selected · to move";
  } else if (mode === "selected" && n === 0) {
    applyEstimate({
      count: 0,
      bytes: 0,
      photos: 0,
      videos: 0,
      photo_bytes: 0,
      video_bytes: 0,
      estimated_batches: 0,
    });
    const bytesLabel = $("archive-est-bytes")?.parentElement?.querySelector("label");
    if (bytesLabel) bytesLabel.textContent = "would leave iCloud";
  }
}

function toggleArchiveAssetSelection(assetId, { forceSelectedMode = true } = {}) {
  if (!assetId) return;
  if (state.archiveSelected.has(assetId)) state.archiveSelected.delete(assetId);
  else state.archiveSelected.add(assetId);
  if (forceSelectedMode && state.archiveSelected.size) switchArchiveModeToSelected();
  updateArchiveSelCount();
  syncArchiveSelectionUi();
}

function syncArchiveSelectionUi() {
  document.querySelectorAll("#archive-grid .archive-card").forEach((card) => {
    const id = card.dataset.id;
    card.classList.toggle("selected", !!id && state.archiveSelected.has(id));
  });
  document.querySelectorAll("#archive-largest-list .archive-largest-row").forEach((row) => {
    const id = row.dataset.id;
    const on = !!id && state.archiveSelected.has(id);
    row.classList.toggle("selected", on);
    const check = row.querySelector(".al-select");
    if (check) check.checked = on;
  });
}

function updateArchiveModeFields() {
  const mode = $("archive-mode")?.value || "keep_years";
  const keep = $("archive-keep-fields");
  const range = $("archive-range-fields");
  if (keep) keep.hidden = mode === "date_range";
  if (range) range.hidden = mode !== "date_range";
}

$("archive-mode")?.addEventListener("change", updateArchiveModeFields);
updateArchiveModeFields();

function setConnPill(text, cls) {
  const el = $("icloud-conn-pill");
  if (!el) return;
  el.textContent = text;
  el.className = `muted-label ${cls || ""}`;
}

async function refreshIcloudStatus() {
  try {
    const s = await api("/api/icloud/status", {
      busy: "Checking iCloud connection…",
    });
    state.icloudActiveAccountId = s.account_id || null;
    renderIcloudAccounts(s.accounts || [], s.account_id);
    if ($("icloud-apple-id")) {
      // Don't wipe what the user is typing unless the field is empty
      if (!$("icloud-apple-id").value) {
        $("icloud-apple-id").value = s.apple_id || "";
      } else if (s.apple_id && document.activeElement !== $("icloud-apple-id")) {
        $("icloud-apple-id").value = s.apple_id;
      }
    }
    const twoFaRow = $("icloud-2fa-row");
    const btn2fa = $("btn-icloud-2fa");
    const btn2faResend = $("btn-icloud-2fa-resend");
    const btnLogin = $("btn-icloud-login");
    let statusText = "";
    let tone = null;
    if (s.requires_2fa) {
      setConnPill(`${s.account_label || "Account"} · 2FA`, "conn-warn");
      if (twoFaRow) twoFaRow.hidden = false;
      if (btn2fa) btn2fa.hidden = false;
      if (btn2faResend) btn2faResend.hidden = false;
      if (btnLogin) btnLogin.hidden = true;
      renderIcloudPhones(s);
      statusText =
        `Asked Apple to text a code` +
        (s.phone_hint ? ` to ${s.phone_hint}` : "") +
        ` for ${s.account_label || "this account"}. Enter it, then Submit 2FA. ` +
        (s.preferred_missing
          ? `Apple does not have a number ending in ${s.preferred_last4} on this Apple ID, so it used the family organizer’s number.`
          : `If no text arrives, pick a number and click Send SMS.`);
    } else if (s.connected) {
      setConnPill(`${s.account_label || "Connected"}`, "conn-ok");
      if (twoFaRow) twoFaRow.hidden = true;
      if (btn2fa) btn2fa.hidden = true;
      if (btn2faResend) btn2faResend.hidden = true;
      if (btnLogin) btnLogin.hidden = false;
      statusText = s.trusted_session
        ? `Connected as ${s.account_label} (${s.apple_id}).`
        : `Connected as ${s.account_label}.`;
      tone = "ok";
    } else {
      setConnPill(s.account_label ? `${s.account_label} · Sign in` : "Not connected", "conn-bad");
      if (twoFaRow) twoFaRow.hidden = true;
      if (btn2fa) btn2fa.hidden = true;
      if (btn2faResend) btn2faResend.hidden = true;
      if (btnLogin) btnLogin.hidden = false;
      statusText =
        s.error || `Select ${s.account_label || "a person"} and sign in with their Apple ID.`;
    }
    // Keep a failed/successful login attempt visible until the next auth action
    if (state.icloudAuthFlash?.message) {
      statusText = state.icloudAuthFlash.message;
      tone = state.icloudAuthFlash.tone || tone;
    }
    setIcloudAuthStatus(statusText, tone);
    updateArchiveAuthLayout(s);
    renderInventoryStatus(s.inventory || {});
    renderArchiveJob(s.archive || {});
    const dest = $("archive-dest");
    if (dest && s.account_label) {
      const cur = (dest.value || "").trim();
      if (!cur || cur === "FromiCloud" || /^FromiCloud\//.test(cur)) {
        dest.value = `FromiCloud/${s.account_label}`;
      }
    }
  } catch (e) {
    setConnPill("Error", "conn-bad");
    setIcloudAuthStatus(e.message, "error");
  }
}

function renderIcloudPhones(s) {
  const sel = $("icloud-2fa-phone");
  const display = $("icloud-2fa-phone-display");
  const help = $("icloud-2fa-phone-help");
  const phones = s.phones || [];
  const selected = s.selected_phone_id;
  const labelFor = (p) =>
    p.last4
      ? `Number ending in ${p.last4}`
      : p.suffix
        ? `Number ending in ${p.suffix}`
        : p.hint || `Trusted number ${p.id}`;
  if (sel) {
    sel.innerHTML = phones.length
      ? phones
          .map((p) => {
            const isSel = selected != null && String(p.id) === String(selected);
            return `<option value="${String(p.id).replace(/"/g, "")}"${isSel ? " selected" : ""}>${labelFor(p)}</option>`;
          })
          .join("")
      : `<option value="">No trusted number listed</option>`;
    sel.hidden = phones.length <= 1;
  }
  if (display) {
    display.hidden = phones.length > 1;
    if (!phones.length) {
      display.textContent = "Apple did not list a trusted number";
    } else if (phones.length === 1) {
      display.textContent = labelFor(phones[0]);
    }
  }
  if (help) {
    if (s.preferred_missing && s.preferred_last4) {
      help.textContent =
        `Apple does not have a number ending in ${s.preferred_last4} on this Apple ID. ` +
        `The code will go to the number shown (often the family organizer). ` +
        `You can’t type a different number here — add it at account.apple.com → Sign-In and Security.`;
    } else if (phones.length > 1) {
      help.textContent = "Choose a trusted number from the list, then Send SMS. You can’t type a new number here.";
    } else {
      help.textContent =
        "Apple only texts a trusted number already on this Apple ID. You can’t type a different number here.";
    }
  }
}

function updateArchiveAuthLayout(s) {
  const expandable = $("icloud-auth-expandable");
  const summary = $("icloud-auth-summary");
  const expandBtn = $("btn-icloud-account-expand");
  const help = $("icloud-auth-help");
  const connected = !!s.connected && !s.requires_2fa;
  const needsAuth = !s.connected || !!s.requires_2fa;

  if (needsAuth && !connected) {
    state.archiveAuthExpanded = true;
  }

  const showForm = needsAuth || state.archiveAuthExpanded;
  if (expandable) expandable.hidden = !showForm;
  if (expandBtn) {
    expandBtn.hidden = !connected;
    expandBtn.textContent = state.archiveAuthExpanded ? "Hide account" : "Account…";
    expandBtn.setAttribute("aria-expanded", showForm && connected ? "true" : "false");
  }
  if (summary) {
    if (connected && !state.archiveAuthExpanded) {
      summary.hidden = false;
      summary.textContent = s.apple_id
        ? `Signed in as ${s.account_label || "account"} · ${s.apple_id}`
        : `Signed in as ${s.account_label || "account"}`;
    } else {
      summary.hidden = true;
    }
  }
  if (help) {
    help.hidden = connected && !state.archiveAuthExpanded;
  }
}

$("btn-icloud-account-expand")?.addEventListener("click", () => {
  state.archiveAuthExpanded = !state.archiveAuthExpanded;
  refreshIcloudStatus();
});

function archiveLsGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    if (v == null) return fallback;
    return v;
  } catch {
    return fallback;
  }
}

function archiveLsSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function setInventoryBodyOpen(open) {
  const body = $("inventory-body");
  const btn = $("btn-inventory-toggle");
  const panel = $("archive-inventory-panel");
  if (body) body.hidden = !open;
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (panel) panel.classList.toggle("is-open", !!open);
}

function initArchiveDisclosurePrefs() {
  const invPref = archiveLsGet("archive.panel.inventory", "0");
  state.archiveInventoryPinned = invPref === "1";
  setInventoryBodyOpen(state.archiveInventoryPinned);

  const opts = $("archive-options");
  if (opts) {
    opts.open = archiveLsGet("archive.panel.options", "0") === "1";
    opts.addEventListener("toggle", () => {
      archiveLsSet("archive.panel.options", opts.open ? "1" : "0");
    });
  }

  const pickTab = archiveLsGet("archive.pick.tab", "largest");
  setArchivePickTab(pickTab === "grid" ? "grid" : "largest", { persist: false });
}

$("btn-inventory-toggle")?.addEventListener("click", () => {
  const body = $("inventory-body");
  const next = !!(body && body.hidden);
  state.archiveInventoryPinned = next;
  archiveLsSet("archive.panel.inventory", next ? "1" : "0");
  setInventoryBodyOpen(next);
});

function setArchivePickTab(tab, { persist = true } = {}) {
  state.archivePickTab = tab === "grid" ? "grid" : "largest";
  const largest = $("archive-pick-largest");
  const grid = $("archive-pick-grid");
  if (largest) largest.hidden = state.archivePickTab !== "largest";
  if (grid) grid.hidden = state.archivePickTab !== "grid";
  document.querySelectorAll(".archive-pick-tab").forEach((btn) => {
    const active = btn.dataset.pickTab === state.archivePickTab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (persist) archiveLsSet("archive.pick.tab", state.archivePickTab);
}

document.querySelectorAll(".archive-pick-tab").forEach((btn) => {
  btn.addEventListener("click", () => setArchivePickTab(btn.dataset.pickTab || "largest"));
});

function setIcloudAuthStatus(message, tone = null) {
  const el = $("icloud-auth-status");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("is-error", tone === "error");
  el.classList.toggle("is-ok", tone === "ok");
}

function setIcloudAuthFlash(message, tone = "error") {
  state.icloudAuthFlash = message ? { message, tone } : null;
  setIcloudAuthStatus(message || "", tone);
}

function renderIcloudAccounts(accounts, activeId) {
  const el = $("icloud-account-list");
  if (!el) return;
  el.innerHTML = "";
  for (const a of accounts) {
    const card = document.createElement("div");
    card.className =
      "icloud-account-card" + (a.active || a.id === activeId ? " active" : "");
    const status = a.has_apple_id
      ? Number(a.asset_count || 0)
        ? `${Number(a.asset_count).toLocaleString()} cached`
        : "Apple ID saved"
      : "Needs sign-in";
    card.innerHTML =
      `<button type="button" class="icloud-account-select">` +
      `<span class="name">${escapeHtml(a.label)}</span>` +
      `<span class="meta">${escapeHtml(status)}</span>` +
      `</button>` +
      `<div class="icloud-account-actions">` +
      `<button type="button" class="btn btn-ghost btn-sm icloud-account-rename" title="Rename">Rename</button>` +
      `<button type="button" class="btn btn-ghost btn-sm icloud-account-delete" title="Remove profile">Remove</button>` +
      `</div>`;
    card.querySelector(".icloud-account-select").addEventListener("click", async () => {
      try {
        state.icloudAuthFlash = null;
        await api("/api/icloud/accounts/select", {
          method: "POST",
          body: JSON.stringify({ account_id: a.id }),
        });
        await refreshIcloudStatus();
        const email = $("icloud-apple-id");
        if (email) {
          if (a.apple_id) email.value = a.apple_id;
          else if (!a.has_apple_id) email.value = "";
          email.focus({ preventScroll: true });
        }
      } catch (err) {
        setIcloudAuthFlash(err.message, "error");
      }
    });
    card.querySelector(".icloud-account-rename").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const next = window.prompt("Rename family account:", a.label);
      if (next == null) return;
      const label = next.trim();
      if (!label || label === a.label) return;
      try {
        await api(`/api/icloud/accounts/${a.id}`, {
          method: "PATCH",
          body: JSON.stringify({ label }),
          busy: "Updating family account…",
        });
        await refreshIcloudStatus();
      } catch (err) {
        setIcloudAuthFlash(err.message, "error");
      }
    });
    card.querySelector(".icloud-account-delete").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (
        !confirm(
          `Remove “${a.label}” from this app?\n\n` +
            "Clears local inventory cache and saved cookies for this profile. " +
            "Does not delete photos from iCloud or the WD drive."
        )
      ) {
        return;
      }
      try {
        await api(`/api/icloud/accounts/${a.id}`, {
          method: "DELETE",
          busy: "Removing family account…",
        });
        await refreshIcloudStatus();
      } catch (err) {
        setIcloudAuthFlash(err.message, "error");
      }
    });
    el.appendChild(card);
  }
}

$("btn-icloud-account-add")?.addEventListener("click", async () => {
  const label = (window.prompt("Name for this family account (e.g. Avery):") || "").trim();
  if (!label) return;
  try {
    await api("/api/icloud/accounts", {
      method: "POST",
      body: JSON.stringify({ label }),
      busy: "Adding family account…",
    });
    await refreshIcloudStatus();
    setIcloudAuthFlash(`Added “${label}” — select them and sign in with their Apple ID.`, "ok");
  } catch (err) {
    setIcloudAuthFlash(err.message, "error");
  }
});

function renderInventoryStatus(inv) {
  const el = $("icloud-inventory-status");
  if (!el) return;
  const running = inv.status === "running";
  const cancelBtn = $("btn-icloud-inventory-cancel");
  if (cancelBtn) cancelBtn.hidden = !running;
  const total = Number(inv.total) || 0;
  const processed = Number(inv.processed) || 0;
  const pct =
    inv.percent != null
      ? Number(inv.percent)
      : total > 0
        ? Math.min(100, Math.round((100 * processed) / total))
        : null;
  const progressCard = $("inventory-progress");
  const pctEl = $("inventory-pct");
  const countsEl = $("inventory-counts");
  const titleEl = $("inventory-progress-title");
  const summaryEl = $("inventory-summary");
  const showProgress =
    running || inv.status === "completed" || inv.status === "cancelled" || inv.status === "error";
  if (progressCard) progressCard.hidden = !showProgress;
  if (titleEl) {
    titleEl.textContent = running
      ? (inv.account_label ? `${inv.account_label} inventory` : "Inventory running")
      : inv.status === "completed"
        ? "Inventory ready"
        : inv.status === "cancelled"
          ? "Inventory cancelled"
          : inv.status === "error"
            ? "Inventory error"
            : "Inventory";
  }
  if (countsEl) {
    if (running && total > 0) {
      countsEl.textContent = `${processed.toLocaleString()} / ${total.toLocaleString()} assets`;
    } else if (running) {
      countsEl.textContent = processed
        ? `${processed.toLocaleString()} assets indexed so far`
        : "Connecting to iCloud…";
    } else if (inv.asset_count != null) {
      countsEl.textContent = `${Number(inv.asset_count).toLocaleString()} assets cached`;
    } else {
      countsEl.textContent = "";
    }
  }
  if (pctEl) {
    if (running && pct != null) {
      pctEl.textContent = `${pct}%`;
      pctEl.classList.remove("is-indet");
    } else if (inv.status === "completed") {
      pctEl.textContent = "100%";
      pctEl.classList.remove("is-indet");
    } else {
      pctEl.textContent = "…";
      pctEl.classList.add("is-indet");
    }
  }
  updateJobProgress($("inventory-bar"), $("inventory-bar-fill"), {
    processed,
    total,
    visible: showProgress,
    indeterminate: running && total <= 0,
  });
  const libBits = Array.isArray(inv.by_library)
    ? inv.by_library.map((l) => {
        const label = l.shared ? "shared" : (l.library_key || "root");
        return `${label}=${Number(l.count || 0).toLocaleString()}`;
      })
    : [];
  const parts = [
    inv.message || null,
    !running && inv.asset_bytes != null ? formatBytes(inv.asset_bytes) : null,
    !running && libBits.length ? `libs=${libBits.join(", ")}` : null,
    !running && inv.refreshed_at ? `refreshed ${inv.refreshed_at}` : null,
    !running && !Number(inv.asset_count || 0) && inv.status !== "error"
      ? "No inventory yet — click Refresh"
      : null,
  ].filter(Boolean);
  el.textContent = parts.join(" · ");

  if (summaryEl) {
    if (running) {
      summaryEl.textContent =
        total > 0
          ? `Refreshing… ${processed.toLocaleString()} / ${total.toLocaleString()}`
          : `Refreshing… ${processed ? processed.toLocaleString() + " so far" : "connecting"}`;
    } else if (Number(inv.asset_count || 0)) {
      const when = inv.refreshed_at
        ? String(inv.refreshed_at).replace("T", " ").slice(0, 16)
        : null;
      summaryEl.textContent =
        `${Number(inv.asset_count).toLocaleString()} cached` +
        (inv.asset_bytes != null ? ` · ${formatBytes(inv.asset_bytes)}` : "") +
        (when ? ` · ${when}` : "");
    } else {
      summaryEl.textContent = "Not refreshed yet";
    }
  }

  // Auto-expand while running; collapse when idle unless user pinned open
  if (running) {
    setInventoryBodyOpen(true);
  } else if (!state.archiveInventoryPinned) {
    setInventoryBodyOpen(false);
  }

  if (running) {
    if (!state.inventoryPoll) state.inventoryPoll = setInterval(pollInventory, 1000);
    pollBusyJobs();
  } else if (state.inventoryPoll) {
    clearInterval(state.inventoryPoll);
    state.inventoryPoll = null;
    pollBusyJobs();
  }
}

async function pollInventory() {
  try {
    const inv = await api("/api/icloud/inventory/status");
    renderInventoryStatus(inv);
  } catch (e) {
    if ($("icloud-inventory-status")) $("icloud-inventory-status").textContent = e.message;
  }
}

function renderArchiveJob(s) {
  const el = $("archive-status");
  const bar = $("archive-bar");
  const fill = $("archive-bar-fill");
  const cancelBtn = $("btn-archive-cancel");
  if (!el) return;
  const running = s.status === "running";
  if (cancelBtn) cancelBtn.hidden = !running;
  updateJobProgress(bar, fill, {
    processed: s.processed,
    total: s.total,
    visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
    indeterminate: running && !(s.total > 0),
  });
  el.textContent = [
    `${s.status || "idle"}${s.phase ? ` / ${s.phase}` : ""}`,
    s.batch_count
      ? `batch ${s.batch_index || 0}/${s.batch_count}`
      : null,
    `processed ${s.processed || 0}/${s.total || 0}`,
    `downloaded=${s.downloaded || 0}`,
    `imported=${s.imported || 0}`,
    `exact_skip=${s.skipped_exact || 0}`,
    `near_skip=${s.skipped_near || 0}`,
    `verified=${s.verified || 0}`,
    `deleted_icloud=${s.deleted_icloud || 0}`,
    `errors=${s.errors || 0}`,
    s.bytes_total
      ? `bytes ${formatBytes(s.bytes_done || 0)} / ${formatBytes(s.bytes_total)}`
      : null,
    s.message || null,
  ]
    .filter(Boolean)
    .join(" · ");
  const report = $("archive-report");
  if (report) {
    report.textContent = (s.report || [])
      .slice(-80)
      .map((r) => JSON.stringify(r))
      .join("\n");
  }
  if (running) {
    if (!state.archivePoll) state.archivePoll = setInterval(pollArchive, 1200);
  } else if (state.archivePoll) {
    clearInterval(state.archivePoll);
    state.archivePoll = null;
    if (s.status === "completed") refreshStats();
  }
}

async function pollArchive() {
  try {
    const s = await api("/api/icloud/archive/status");
    renderArchiveJob(s);
  } catch (e) {
    if ($("archive-status")) $("archive-status").textContent = e.message;
  }
}

$("btn-icloud-password-toggle")?.addEventListener("click", (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  const input = $("icloud-password");
  const revealed = input?.closest(".password-field")?.classList.contains("is-revealed");
  setPasswordRevealed(!revealed);
});

async function submitIcloudLogin(ev) {
  if (ev) ev.preventDefault();
  const twoFaRow = $("icloud-2fa-row");
  const code = ($("icloud-2fa")?.value || "").trim();
  if (twoFaRow && !twoFaRow.hidden) {
    if (code) {
      await submitIcloud2fa();
      return;
    }
    setIcloudAuthFlash(
      "Sign-in already sent an SMS. Enter that code and click Submit 2FA — do not sign in again.",
      "error"
    );
    $("icloud-2fa")?.focus();
    return;
  }
  const btn = $("btn-icloud-login");
  const appleId = ($("icloud-apple-id")?.value || "").trim();
  const password = $("icloud-password")?.value || "";
  if (!appleId || !password) {
    setIcloudAuthFlash("Enter Apple ID and password, or pick them from Google Passwords (key icon).", "error");
    return;
  }
  try {
    if (btn) btn.disabled = true;
    setIcloudAuthFlash(`Signing in as ${appleId}…`, "ok");
    // Keep type=password during submit so Chrome can offer to save the password for this site
    setPasswordRevealed(false);
    const data = await api("/api/icloud/auth", {
      method: "POST",
      body: JSON.stringify({
        apple_id: appleId,
        password,
        account_id: state.icloudActiveAccountId || undefined,
      }),
    });
    if (data.status === "2fa_required") {
      setIcloudAuthFlash(
        data.message ||
          `Sign-in needs 2FA for ${data.apple_id || appleId}. Enter the code from a trusted Apple device.`,
        "ok"
      );
      $("icloud-2fa-row").hidden = false;
      $("btn-icloud-2fa").hidden = false;
      if ($("btn-icloud-2fa-resend")) $("btn-icloud-2fa-resend").hidden = false;
      if ($("btn-icloud-login")) $("btn-icloud-login").hidden = true;
      renderIcloudPhones(data);
      $("icloud-2fa")?.focus();
    } else if (data.status === "connected") {
      $("icloud-password").value = "";
      setPasswordRevealed(false);
      setIcloudAuthFlash(
        `${data.message || "Signed in successfully."} Next: click Refresh inventory to index this library.`,
        "ok"
      );
    } else {
      setIcloudAuthFlash(data.message || `Sign-in response: ${data.status}`, "ok");
    }
    await refreshIcloudStatus();
  } catch (e) {
    // Keep password so Google Passwords / typo fixes still work
    setIcloudAuthFlash(`Sign-in failed: ${e.message}`, "error");
    setConnPill("Sign-in failed", "conn-bad");
  } finally {
    if (btn) btn.disabled = false;
  }
}

$("icloud-login-form")?.addEventListener("submit", submitIcloudLogin);

async function submitIcloud2fa() {
  try {
    setIcloudAuthFlash("Submitting 2FA…", "ok");
    const data = await api("/api/icloud/auth/2fa", {
      method: "POST",
      body: JSON.stringify({ code: $("icloud-2fa").value.trim() }),
    });
    $("icloud-2fa").value = "";
    $("icloud-password").value = "";
    setPasswordRevealed(false);
    setIcloudAuthFlash(
      `${data.message || "Signed in."} Next: click Refresh inventory to index photos/videos.`,
      "ok"
    );
    await refreshIcloudStatus();
  } catch (e) {
    setIcloudAuthFlash(`2FA failed: ${e.message}`, "error");
    setConnPill("2FA failed", "conn-bad");
    $("icloud-2fa")?.focus();
  }
}

$("btn-icloud-2fa")?.addEventListener("click", () => {
  void submitIcloud2fa();
});

function setPasswordRevealed(reveal) {
  const input = $("icloud-password");
  const btn = $("btn-icloud-password-toggle");
  const wrap = input?.closest(".password-field");
  if (!input || !wrap) return;
  wrap.classList.toggle("is-revealed", !!reveal);
  input.setAttribute("type", reveal ? "text" : "password");
  input.type = reveal ? "text" : "password";
  if (btn) {
    btn.setAttribute("aria-pressed", reveal ? "true" : "false");
    btn.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    btn.title = reveal ? "Hide password" : "Show password";
    const label = btn.querySelector(".password-toggle-label");
    if (label) label.textContent = reveal ? "Hide" : "Show";
  }
}

$("btn-icloud-2fa-resend")?.addEventListener("click", async () => {
  try {
    setIcloudAuthFlash("Requesting a fresh 2FA code…", "ok");
    const data = await api("/api/icloud/auth/2fa/resend", {
      method: "POST",
      body: "{}",
    });
    setIcloudAuthFlash(data.message || "Fresh 2FA code requested.", "ok");
    $("icloud-2fa")?.focus();
  } catch (e) {
    setIcloudAuthFlash(`Resend failed: ${e.message}`, "error");
  }
});

$("btn-icloud-2fa-sms")?.addEventListener("click", async () => {
  try {
    setIcloudAuthFlash("Requesting an SMS code…", "ok");
    const phoneId = $("icloud-2fa-phone")?.value;
    const data = await api("/api/icloud/auth/2fa/sms", {
      method: "POST",
      body: JSON.stringify({ phone_id: phoneId || undefined }),
    });
    renderIcloudPhones(data);
    setIcloudAuthFlash(data.message || "SMS code requested.", "ok");
    $("icloud-2fa")?.focus();
  } catch (e) {
    setIcloudAuthFlash(`SMS 2FA failed: ${e.message}`, "error");
  }
});

$("btn-icloud-logout")?.addEventListener("click", async () => {
  try {
    await api("/api/icloud/logout", { method: "POST", body: "{}" });
    if ($("icloud-password")) $("icloud-password").value = "";
    if ($("icloud-2fa")) $("icloud-2fa").value = "";
    setPasswordRevealed(false);
    state.archiveAuthExpanded = true;
    setIcloudAuthFlash("Signed out. Enter Apple ID and password to sign in again.", "ok");
    await refreshIcloudStatus();
  } catch (e) {
    setIcloudAuthFlash(`Sign-out failed: ${e.message}`, "error");
  }
});

$("btn-icloud-reset")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/icloud/auth/reset", { method: "POST", body: "{}" });
    if ($("icloud-password")) $("icloud-password").value = "";
    if ($("icloud-2fa")) $("icloud-2fa").value = "";
    setPasswordRevealed(false);
    state.archiveAuthExpanded = true;
    setIcloudAuthFlash(
      data.message || "Session cleared. Enter Apple ID and password, then Sign in.",
      "ok"
    );
    await refreshIcloudStatus();
  } catch (e) {
    setIcloudAuthFlash(`Reset failed: ${e.message}`, "error");
  }
});

$("btn-icloud-inventory")?.addEventListener("click", async () => {
  try {
    if ($("icloud-inventory-status")) {
      $("icloud-inventory-status").textContent = "Starting inventory refresh…";
    }
    const inv = await api("/api/icloud/inventory/refresh", {
      method: "POST",
      body: "{}",
      busy: "Starting inventory refresh…",
    });
    renderInventoryStatus(inv);
    pollBusyJobs();
  } catch (e) {
    $("icloud-inventory-status").textContent = e.message;
  }
});

$("btn-icloud-inventory-cancel")?.addEventListener("click", async () => {
  try {
    const inv = await api("/api/icloud/inventory/cancel", { method: "POST", body: "{}" });
    renderInventoryStatus(inv);
  } catch (e) {
    $("icloud-inventory-status").textContent = e.message;
  }
});

function applyEstimate(data) {
  if ($("archive-est-count")) $("archive-est-count").textContent = Number(data.count || 0).toLocaleString();
  if ($("archive-est-bytes")) $("archive-est-bytes").textContent = formatBytes(data.bytes || 0);
  if ($("archive-est-photos")) {
    $("archive-est-photos").textContent =
      `${Number(data.photos || 0).toLocaleString()} (${formatBytes(data.photo_bytes || 0)})`;
  }
  if ($("archive-est-videos")) {
    $("archive-est-videos").textContent =
      `${Number(data.videos || 0).toLocaleString()} (${formatBytes(data.video_bytes || 0)})`;
  }
  if ($("archive-est-batches")) {
    $("archive-est-batches").textContent = String(data.estimated_batches || 0);
  }
  const bytesLabel = $("archive-est-bytes")?.parentElement?.querySelector("label");
  if (bytesLabel && $("archive-mode")?.value !== "selected") {
    bytesLabel.textContent = "would leave iCloud";
  }
}

$("btn-archive-estimate")?.addEventListener("click", async () => {
  try {
    if ($("archive-status")) $("archive-status").textContent = "Checking inventory…";
    const inv = await api("/api/icloud/inventory/status", {
      busy: "Checking inventory before estimate…",
    });
    if (!Number(inv.asset_count || 0)) {
      throw new Error(
        `${inv.account_label || "This account"} has no inventory yet. Click Refresh inventory first.`
      );
    }
    const body = archiveFilterBody();
    if (body.asset_ids && !body.asset_ids.length) {
      throw new Error("Select assets in the grid first (or switch filter mode).");
    }
    if ($("archive-status")) $("archive-status").textContent = "Estimating space…";
    const data = await api("/api/icloud/archive/estimate", {
      method: "POST",
      body: JSON.stringify(body),
      busy: "Estimating archive space…",
    });
    applyEstimate(data);
    $("archive-status").textContent =
      `Estimate ready · cutoff=${data.cutoff || "range/selection"} · inventory=${Number(data.inventory_total || 0).toLocaleString()}`;
  } catch (e) {
    $("archive-status").textContent = e.message;
  }
});

function renderArchiveGrid(assets) {
  const grid = $("archive-grid");
  if (!grid) return;
  rememberArchiveAssetSizes(assets);
  grid.innerHTML = "";
  if (!assets.length) {
    grid.innerHTML = `<div class="empty">No matching assets for this filter.</div>`;
    return;
  }
  for (const a of assets) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "archive-card" + (state.archiveSelected.has(a.asset_id) ? " selected" : "");
    btn.dataset.id = a.asset_id;
    const created = (a.created_at || "").slice(0, 10) || "unknown date";
    const typeLabel = `${a.item_type || "image"}${a.is_live_photo ? " · live" : ""}`;
    btn.innerHTML = `
      <div class="ac-thumb-wrap">
        <img class="ac-thumb" alt="" loading="lazy" src="/api/icloud/assets/${encodeURIComponent(a.asset_id)}/thumb" />
        <span class="ac-type">${typeLabel}</span>
      </div>
      <span class="ac-name" title="${a.filename || ""}">${a.filename || a.asset_id}</span>
      <span class="ac-meta">${created}</span>
      <span class="ac-meta">${formatBytes(a.size || 0)}</span>
    `;
    const img = btn.querySelector(".ac-thumb");
    if (img) {
      img.addEventListener("error", () => {
        img.classList.add("missing");
        img.removeAttribute("src");
        img.alt = a.item_type === "video" ? "VIDEO" : "No preview";
      });
    }
    btn.addEventListener("click", () => {
      toggleArchiveAssetSelection(a.asset_id);
    });
    grid.appendChild(btn);
  }
  updateArchiveSelCount();
  const label = $("archive-pager-label");
  if (label) {
    const start = state.archiveTotal ? state.archiveOffset + 1 : 0;
    const end = Math.min(state.archiveOffset + state.archiveLimit, state.archiveTotal);
    label.textContent = state.archiveTotal
      ? `${start}–${end} of ${state.archiveTotal.toLocaleString()}`
      : "";
  }
}

function formatArchiveDate(iso) {
  if (!iso) return { absolute: "Unknown date", relative: "" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return { absolute: String(iso).slice(0, 10), relative: "" };
  }
  const absolute = d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const days = Math.max(0, Math.round((Date.now() - d.getTime()) / 86400000));
  let relative = "";
  if (days < 1) relative = "today";
  else if (days < 30) relative = `${days}d ago`;
  else if (days < 365) relative = `${Math.round(days / 30)}mo ago`;
  else {
    const years = days / 365.25;
    relative = years >= 10 ? `${Math.round(years)}y ago` : `${years.toFixed(1)}y ago`;
  }
  return { absolute, relative };
}

function openIcloudAssetPreview(a) {
  const lightbox = $("lightbox");
  const img = $("lightbox-img");
  const vid = $("lightbox-video");
  const meta = $("lightbox-meta");
  if (!lightbox || !img) return;

  const name = a.filename || a.asset_id || "";
  const isVideo =
    a.item_type === "video" ||
    /\.(mov|mp4|m4v)$/i.test(name);
  const mediaUrl = `/api/icloud/assets/${encodeURIComponent(a.asset_id)}/media`;
  const { absolute, relative } = formatArchiveDate(a.created_at);
  const typeLabel =
    (isVideo ? "Video" : a.is_live_photo ? "Live photo" : "Photo") +
    (a.library_key && a.library_key !== "root" ? ` · ${a.library_key}` : "");

  if (vid) {
    vid.pause();
    vid.removeAttribute("src");
    vid.classList.add("hidden");
  }

  lightbox.classList.remove("hidden");

  if (isVideo && vid) {
    img.classList.add("hidden");
    img.removeAttribute("src");
    vid.classList.remove("hidden");
    vid.controls = true;
    vid.playsInline = true;
    vid.src = mediaUrl;
    vid.currentTime = 0;
    const play = () => {
      const attempt = vid.play();
      if (attempt && typeof attempt.catch === "function") {
        attempt.catch(() => {
          /* autoplay blocked — controls still work */
        });
      }
    };
    vid.addEventListener("loadeddata", play, { once: true });
    vid.addEventListener(
      "error",
      () => {
        if (meta) {
          meta.textContent =
            `${name} · could not play in-browser (codec?). Try Archive to WD, then open the file locally.`;
        }
      },
      { once: true }
    );
  } else {
    if (vid) {
      vid.pause();
      vid.removeAttribute("src");
      vid.classList.add("hidden");
    }
    img.classList.remove("hidden");
    img.src = mediaUrl;
    img.onerror = () => {
      img.onerror = null;
      img.src = `/api/icloud/assets/${encodeURIComponent(a.asset_id)}/thumb`;
    };
  }

  if (meta) {
    meta.textContent = [
      name,
      typeLabel,
      absolute + (relative ? ` (${relative})` : ""),
      formatBytes(a.size || 0),
      isVideo
        ? "Streaming from iCloud — large files may take a moment to start"
        : "Streaming from iCloud",
    ].join(" · ");
  }
}

function renderLargestList(assets, meta = {}) {
  const list = $("archive-largest-list");
  const metaEl = $("archive-largest-meta");
  if (!list) return;
  state.archiveLargestAssets = assets || [];
  rememberArchiveAssetSizes(assets);
  list.innerHTML = "";
  if (!assets.length) {
    list.innerHTML = `<div class="empty">No assets match this largest filter. Refresh inventory or widen the scope.</div>`;
    if (metaEl) metaEl.textContent = "0 listed";
    updateArchiveSelCount();
    return;
  }
  assets.forEach((a, i) => {
    const row = document.createElement("div");
    row.className =
      "archive-largest-row" + (state.archiveSelected.has(a.asset_id) ? " selected" : "");
    row.dataset.id = a.asset_id;
    const { absolute, relative } = formatArchiveDate(a.created_at);
    const isVideo = a.item_type === "video";
    const typeLabel = isVideo ? "Video" : a.is_live_photo ? "Live photo" : "Photo";
    const lib =
      a.library_key && String(a.library_key).startsWith("shared")
        ? "Shared library"
        : "Private library";
    row.innerHTML = `
      <label class="al-check" title="Select for archive">
        <input type="checkbox" class="al-select" ${state.archiveSelected.has(a.asset_id) ? "checked" : ""} />
        <span class="al-rank">#${i + 1}</span>
      </label>
      <button type="button" class="al-thumb-btn" title="Preview">
        <span class="al-thumb-wrap">
          <img class="al-thumb" alt="" loading="lazy" src="/api/icloud/assets/${encodeURIComponent(a.asset_id)}/thumb" />
          <span class="al-type-badge">${isVideo ? "VIDEO" : "PHOTO"}</span>
        </span>
      </button>
      <div class="al-body">
        <span class="al-name" title="${escapeHtml(a.filename || "")}">${escapeHtml(a.filename || a.asset_id)}</span>
        <span class="al-meta"><strong>${escapeHtml(typeLabel)}</strong> · ${escapeHtml(absolute)}${relative ? ` · ${escapeHtml(relative)}` : ""}</span>
        <span class="al-meta">${escapeHtml(lib)} · ${formatBytes(a.size || 0)}</span>
      </div>
      <div class="al-side">
        <span class="al-size">${formatBytes(a.size || 0)}</span>
        <button type="button" class="btn btn-ghost btn-sm al-preview-btn">Preview</button>
      </div>
    `;
    const img = row.querySelector(".al-thumb");
    if (img) {
      img.addEventListener("error", () => {
        img.classList.add("missing");
        img.removeAttribute("src");
        img.alt = isVideo ? "VIDEO" : "No preview";
      });
    }
    row.querySelector(".al-select")?.addEventListener("change", (ev) => {
      const checked = !!ev.target.checked;
      if (checked) state.archiveSelected.add(a.asset_id);
      else state.archiveSelected.delete(a.asset_id);
      if (checked) switchArchiveModeToSelected();
      row.classList.toggle("selected", checked);
      updateArchiveSelCount();
      syncArchiveSelectionUi();
    });
    const preview = () => openIcloudAssetPreview(a);
    row.querySelector(".al-thumb-btn")?.addEventListener("click", preview);
    row.querySelector(".al-preview-btn")?.addEventListener("click", preview);
    list.appendChild(row);
  });
  if (metaEl) {
    const listed = assets.length;
    const matching = Number(meta.total_matching || listed);
    const bytes = Number(meta.bytes_listed || 0);
    metaEl.textContent =
      `${listed} listed · ${formatBytes(bytes)}` +
      (matching > listed ? ` · of ${matching.toLocaleString()} matching` : "");
  }
  updateArchiveSelCount();
}

async function loadLargestAssets() {
  try {
    if ($("archive-status")) $("archive-status").textContent = "Loading largest assets…";
    const entire = !!$("archive-largest-entire")?.checked;
    const body = {
      ...(entire ? {} : archiveScopeFilterBody()),
      entire_inventory: entire,
      media: $("archive-largest-media")?.value || "all",
      limit: Number($("archive-largest-limit")?.value) || 50,
    };
    const data = await api("/api/icloud/archive/largest", {
      method: "POST",
      body: JSON.stringify(body),
      busy: "Loading largest iCloud assets…",
    });
    renderLargestList(data.assets || [], data);
    if ($("archive-status")) {
      $("archive-status").textContent =
        data.entire_inventory
          ? `Largest from entire inventory (${data.account_label || "account"}).`
          : `Largest outside keep window` +
            (data.cutoff ? ` (before ${String(data.cutoff).slice(0, 10)})` : "") +
            ".";
    }
  } catch (e) {
    if ($("archive-status")) $("archive-status").textContent = e.message;
  }
}

$("btn-archive-largest")?.addEventListener("click", () => loadLargestAssets());
$("btn-archive-largest-select")?.addEventListener("click", () => {
  if (!state.archiveLargestAssets.length) {
    alert("Show largest first.");
    return;
  }
  for (const a of state.archiveLargestAssets) {
    state.archiveSelected.add(a.asset_id);
  }
  switchArchiveModeToSelected();
  updateArchiveSelCount();
  syncArchiveSelectionUi();
});

async function loadArchiveGrid() {
  try {
    if ($("archive-status")) $("archive-status").textContent = "Loading matching grid…";
    const body = archiveFilterBody();
    // For grid preview in selected mode without selection, fall back to keep window
    let filter = body;
    if (body.asset_ids) {
      filter = {
        keep_years: Number($("archive-keep-value").value) || 2,
      };
      if ($("archive-keep-unit").value === "months") {
        filter = { keep_months: Number($("archive-keep-value").value) || 24 };
      }
    }
    const data = await api(
      `/api/icloud/archive/assets?limit=${state.archiveLimit}&offset=${state.archiveOffset}`,
      {
        method: "POST",
        body: JSON.stringify(filter),
        busy: "Loading matching iCloud assets…",
      }
    );
    state.archiveTotal = data.total || 0;
    renderArchiveGrid(data.assets || []);
    applyEstimate({
      count: data.total,
      bytes: (data.assets || []).reduce((s, a) => s + (a.size || 0), 0),
      photos: (data.assets || []).filter((a) => a.item_type !== "video").length,
      videos: (data.assets || []).filter((a) => a.item_type === "video").length,
      photo_bytes: 0,
      video_bytes: 0,
      estimated_batches: Math.ceil((data.total || 0) / 500),
    });
    if ($("archive-status")) {
      $("archive-status").textContent = data.total
        ? `Grid ready · ${Number(data.total).toLocaleString()} matching assets`
        : "No matching assets for this filter.";
    }
  } catch (e) {
    $("archive-status").textContent = e.message;
  }
}

$("btn-archive-load-grid")?.addEventListener("click", () => {
  state.archiveOffset = 0;
  loadArchiveGrid();
});

$("btn-archive-prev")?.addEventListener("click", () => {
  state.archiveOffset = Math.max(0, state.archiveOffset - state.archiveLimit);
  loadArchiveGrid();
});

$("btn-archive-next")?.addEventListener("click", () => {
  if (state.archiveOffset + state.archiveLimit < state.archiveTotal) {
    state.archiveOffset += state.archiveLimit;
    loadArchiveGrid();
  }
});

$("btn-archive-select-page")?.addEventListener("click", () => {
  document.querySelectorAll("#archive-grid .archive-card").forEach((card) => {
    const id = card.dataset.id;
    if (id) {
      state.archiveSelected.add(id);
      card.classList.add("selected");
    }
  });
  switchArchiveModeToSelected();
  updateArchiveSelCount();
  syncArchiveSelectionUi();
});

$("btn-archive-clear-sel")?.addEventListener("click", () => {
  state.archiveSelected.clear();
  updateArchiveSelCount();
  syncArchiveSelectionUi();
});

$("btn-archive-start")?.addEventListener("click", async () => {
  try {
    if ($("archive-status")) $("archive-status").textContent = "Starting archive…";
    const body = {
      ...archiveFilterBody(),
      delete_after: !!$("archive-delete")?.checked,
      confirm_delete: false,
      dest_folder: $("archive-dest")?.value.trim() || "FromiCloud",
      check_near: !!$("archive-near")?.checked,
    };
    if (body.asset_ids && !body.asset_ids.length) {
      throw new Error("Select assets in the grid first (or switch filter mode).");
    }
    const stats = selectedArchiveStats();
    const sizeLine =
      body.asset_ids && stats.count
        ? `\n\nSelection: ${stats.count.toLocaleString()} assets · ${formatBytes(stats.bytes)}` +
          (stats.videos
            ? ` (${stats.videos.toLocaleString()} videos / ${stats.photos.toLocaleString()} photos)`
            : "")
        : "";
    if (body.delete_after) {
      const token = promptConfirmToken(
        CONFIRM_DELETE_ICLOUD,
        "This will download matching photos to the WD, verify them, then move them to iCloud Recently Deleted (~30 days).\n\n" +
          "Recent photos in your keep window stay on iCloud." +
          sizeLine
      );
      if (!token) {
        if ($("archive-status")) $("archive-status").textContent = "Archive cancelled.";
        return;
      }
      body.confirm_delete = true;
      body.confirm_token = token;
    } else {
      const ok = confirm(
        "Download matching photos to the WD (no iCloud delete)." + sizeLine + "\n\nContinue?"
      );
      if (!ok) {
        if ($("archive-status")) $("archive-status").textContent = "Archive cancelled.";
        return;
      }
    }
    const s = await api("/api/icloud/archive/start", {
      method: "POST",
      body: JSON.stringify(body),
      busy: "Starting archive to WD…",
    });
    renderArchiveJob(s);
    pollBusyJobs();
  } catch (e) {
    if ($("archive-status")) $("archive-status").textContent = e.message;
  }
});

$("btn-archive-cancel")?.addEventListener("click", async () => {
  try {
    const s = await api("/api/icloud/archive/cancel", { method: "POST", body: "{}" });
    renderArchiveJob(s);
  } catch (e) {
    $("archive-status").textContent = e.message;
  }
});

/* Organize */
let organizePoll = null;

function renderOrganizeStatus(s) {
  const el = $("org-status");
  const cancelBtn = $("btn-org-cancel");
  const running = s.status === "running";
  if (cancelBtn) cancelBtn.hidden = !running;
  updateJobProgress($("org-bar"), $("org-bar-fill"), {
    processed: s.processed,
    total: s.total,
    visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
    indeterminate: running && !(s.total > 0),
  });
  if (el) {
    const pct =
      s.total > 0 ? Math.round((100 * (s.processed || 0)) / s.total) : null;
    el.textContent = [
      `${s.status}${s.phase ? ` / ${s.phase}` : ""}`,
      s.total ? `${s.processed || 0}/${s.total}${pct != null ? ` (${pct}%)` : ""}` : null,
      `moved=${s.moved || s.moved_count || 0}`,
      `skipped=${s.skipped || s.skipped_count || 0}`,
      s.missing || s.missing_count
        ? `missing=${s.missing || s.missing_count}`
        : null,
      s.folders_removed ? `empty_dirs_removed=${s.folders_removed}` : null,
      `errors=${s.errors || s.error_count || 0}`,
      s.message || null,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  const report = $("org-report");
  if (report) {
    const lines = (s.report || []).slice(-60).map((r) => JSON.stringify(r));
    report.hidden = !lines.length;
    report.textContent = lines.join("\n");
  }
}

async function pollOrganize() {
  try {
    const s = await api("/api/organize/status");
    renderOrganizeStatus(s);
    if (s.status === "running") {
      if (!organizePoll) organizePoll = setInterval(pollOrganize, 1500);
      pollBusyJobs();
    } else if (organizePoll) {
      clearInterval(organizePoll);
      organizePoll = null;
      pollBusyJobs();
      if (s.status === "completed" || s.status === "cancelled") {
        refreshStats();
        loadFilters();
        loadPhotos();
      }
    }
  } catch (e) {
    if ($("org-status")) $("org-status").textContent = e.message;
  }
}

$("btn-org-all").addEventListener("click", async () => {
  const consolidate = !!$("org-consolidate")?.checked;
  const removeEmpty = !!$("org-remove-empty")?.checked;
  const undatedFolder = ($("org-undated-folder")?.value || "Undated").trim();
  const pattern = $("org-pattern").value;
  const msg = consolidate
    ? "Consolidate the whole library into dated folders?\n\n" +
      "• Moves photos out of Google Photos, drive root, albums, etc.\n" +
      `• Pattern: ${pattern}\n` +
      `• No camera/EXIF date → ${undatedFolder || "Undated"}/ (file modified date is not used)\n` +
      (removeEmpty ? "• Removes empty legacy folders when done\n" : "") +
      "\nThis can take a long time over SMB. Resume scan first if you haven't recently."
    : "Move all photos with camera/EXIF dates into dated folders?\n\n" +
      "Photos without EXIF dates are skipped (or go to Undated/ when consolidating).\n" +
      "Photos already in the correct dated folder are skipped.";
  if (!confirm(msg)) return;
  const el = $("org-status");
  if (el) el.textContent = consolidate ? "Starting consolidate…" : "Starting organize…";
  try {
    const s = await api("/api/organize/by-date", {
      method: "POST",
      body: JSON.stringify({
        pattern,
        confirm: true,
        consolidate,
        undated_folder: undatedFolder || "Undated",
        remove_empty_folders: removeEmpty && consolidate,
      }),
    });
    renderOrganizeStatus(s);
    pollOrganize();
    pollBusyJobs();
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

$("btn-org-cancel")?.addEventListener("click", async () => {
  await requestJobCancel("/api/organize/cancel", "Organize");
});

$("btn-org-purge-preview")?.addEventListener("click", async () => {
  const el = $("org-purge-status");
  if (el) el.textContent = "Checking inaccessible files…";
  try {
    const data = await api("/api/organize/inaccessible/preview", {
      method: "POST",
      body: JSON.stringify({
        folder_prefix: ($("org-purge-prefix")?.value || "").trim() || null,
      }),
    });
    if (el) {
      el.textContent =
        `Checked ${data.checked} · ok=${data.ok} · denied=${data.denied} · missing=${data.missing}` +
        (data.folder_prefix ? ` · under “${data.folder_prefix}”` : "");
    }
    const report = $("org-report");
    if (report) {
      report.hidden = false;
      report.textContent = JSON.stringify(data.sample || [], null, 2);
    }
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

$("btn-org-purge")?.addEventListener("click", async () => {
  const prefix = ($("org-purge-prefix")?.value || "").trim() || "Google Drive Old";
  if (
    !confirm(
      `Remove inaccessible files under “${prefix}” from the library index?\n\n` +
        "Files are NOT deleted from My Cloud — only ghost entries that Windows cannot open."
    )
  ) {
    return;
  }
  const el = $("org-purge-status");
  if (el) el.textContent = "Purging inaccessible index rows…";
  try {
    const data = await api("/api/organize/inaccessible/purge", {
      method: "POST",
      body: JSON.stringify({ confirm: true, folder_prefix: prefix }),
    });
    if (el) {
      el.textContent =
        `Removed ${data.removed} from index (checked ${data.checked}, readable ${data.readable}).`;
    }
    const report = $("org-report");
    if (report) {
      report.hidden = false;
      report.textContent = JSON.stringify(data.sample || [], null, 2);
    }
    refreshStats();
    loadFilters();
    loadPhotos();
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

/* RAW → JPEG convert */
let rawConvertPoll = null;

function renderRawConvertStatus(s) {
  const el = $("raw-status");
  const cancelBtn = $("btn-raw-cancel");
  const running = s.status === "running";
  if (cancelBtn) cancelBtn.hidden = !running;
  updateJobProgress($("raw-bar"), $("raw-bar-fill"), {
    processed: s.processed,
    total: s.total,
    visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
    indeterminate: running && !(s.total > 0),
  });
  if (el) {
    el.textContent = [
      `${s.status}${s.phase ? ` / ${s.phase}` : ""}`,
      s.total ? `${s.processed || 0}/${s.total}` : null,
      `converted=${s.converted || 0}`,
      `quarantined=${s.quarantined || 0}`,
      `skipped=${s.skipped || 0}`,
      `errors=${s.errors || 0}`,
      s.message || null,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  const report = $("raw-report");
  if (report) {
    const lines = (s.report || []).slice(-60).map((r) => JSON.stringify(r));
    report.hidden = !lines.length;
    report.textContent = lines.join("\n");
  }
}

async function pollRawConvert() {
  try {
    const s = await api("/api/raw/status");
    renderRawConvertStatus(s);
    if (s.status === "running") {
      if (!rawConvertPoll) rawConvertPoll = setInterval(pollRawConvert, 1500);
      pollBusyJobs();
    } else if (rawConvertPoll) {
      clearInterval(rawConvertPoll);
      rawConvertPoll = null;
      pollBusyJobs();
      if (s.status === "completed" || s.status === "cancelled") {
        refreshStats();
        loadFilters();
        loadPhotos();
      }
    }
  } catch (e) {
    if ($("raw-status")) $("raw-status").textContent = e.message;
  }
}

$("btn-raw-preview")?.addEventListener("click", async () => {
  const el = $("raw-status");
  if (el) el.textContent = "Scanning for RAW files (may take a while on SMB)…";
  try {
    const data = await api("/api/raw/preview", { method: "POST", body: "{}" });
    if (el) {
      const exts = Object.entries(data.by_ext || {})
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
      el.textContent =
        `Found ${data.count} RAW files (${formatBytes(data.bytes || 0)})` +
        (exts ? ` · ${exts}` : "") +
        ` · quarantine → ${data.quarantine_dir || "_quarantine_raw"}`;
    }
    const report = $("raw-report");
    if (report) {
      report.hidden = false;
      report.textContent = (data.sample || []).join("\n");
    }
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

$("btn-raw-start")?.addEventListener("click", async () => {
  const quarantine = !!$("raw-quarantine")?.checked;
  const pattern = $("raw-pattern")?.value || "YYYY/YYYY-MM";
  if (
    !confirm(
      "Convert RAW / misnamed camera files to JPEG and place them in dated folders?\n\n" +
        `Pattern: ${pattern}\n` +
        (quarantine
          ? "Originals move to _quarantine_raw (recoverable).\n"
          : "Originals stay in place.\n") +
        "\nThis can take a long time over SMB."
    )
  ) {
    return;
  }
  const el = $("raw-status");
  if (el) el.textContent = "Starting convert…";
  try {
    const s = await api("/api/raw/start", {
      method: "POST",
      body: JSON.stringify({ confirm: true, pattern, quarantine }),
    });
    renderRawConvertStatus(s);
    pollRawConvert();
    pollBusyJobs();
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

$("btn-raw-cancel")?.addEventListener("click", async () => {
  await requestJobCancel("/api/raw/cancel", "File Converter");
});

$("btn-move").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) {
    alert("Select photos in Browse first.");
    return;
  }
  const dest = $("move-dest").value.trim();
  if (!dest) {
    alert("Enter a destination folder.");
    return;
  }
  try {
    const data = await api("/api/organize/move", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, dest_folder: dest }),
    });
    $("org-status").textContent = `Moved ${data.moved.length}, errors ${data.errors.length}`;
    state.selected.clear();
    $("sel-count").textContent = "0";
    refreshStats();
    loadFilters();
    loadPhotos();
  } catch (e) {
    $("org-status").textContent = e.message;
  }
});

/* Settings */
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("photo-root").value = s.photo_root || "";
    const workers = $("scan-workers");
    if (workers) workers.value = String(s.scan_workers ?? 3);
    $("settings-status").textContent = s.configured
      ? `Configured · ${s.scan_workers} worker${s.scan_workers === 1 ? "" : "s"} · v${s.version}`
      : `Not configured — set a path that exists on this PC.`;
  } catch (e) {
    $("settings-status").textContent = e.message;
  }
}

$("btn-save-settings").addEventListener("click", async () => {
  try {
    const workersEl = $("scan-workers");
    const workers = workersEl ? Number(workersEl.value) : 3;
    const s = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        photo_root: $("photo-root").value.trim(),
        scan_workers: workers,
      }),
    });
    $("settings-status").textContent =
      `Saved: ${s.photo_root} · ${s.scan_workers} worker${s.scan_workers === 1 ? "" : "s"} (next scan)`;
  } catch (e) {
    $("settings-status").textContent = e.message;
  }
});

/* Junk / corrupt cleanup */
let cleanupPoll = null;

async function pollCleanup() {
  try {
    const s = await api("/api/cleanup/status");
    const el = $("cleanup-status");
    const cancelBtn = $("btn-cleanup-cancel");
    const running = s.status === "running";
    if (cancelBtn) cancelBtn.hidden = !running;
    const deleting = s.phase === "deleting" && s.total > 0;
    updateJobProgress($("cleanup-bar"), $("cleanup-bar-fill"), {
      processed: deleting ? s.processed : s.scanned,
      total: deleting ? s.total : 0,
      visible: running || s.status === "completed" || s.status === "cancelled" || s.status === "error",
      indeterminate: running && !deleting,
    });
    if (el) {
      el.textContent = [
        `${s.status}${s.phase ? ` / ${s.phase}` : ""}`,
        deleting
          ? `${s.processed || 0}/${s.total}`
          : s.scanned
            ? `scanned=${s.scanned}`
            : null,
        `deleted=${s.deleted || 0}`,
        s.bytes_freed ? `freed=${formatBytes(s.bytes_freed)}` : null,
        `errors=${s.errors || 0}`,
        s.message || null,
      ]
        .filter(Boolean)
        .join(" · ");
    }
    const report = $("cleanup-report");
    if (report) {
      report.textContent = (s.report || [])
        .slice(-80)
        .map((r) => JSON.stringify(r))
        .join("\n");
    }
    if (running) {
      if (!cleanupPoll) cleanupPoll = setInterval(pollCleanup, 1500);
      pollBusyJobs();
    } else if (cleanupPoll) {
      clearInterval(cleanupPoll);
      cleanupPoll = null;
      pollBusyJobs();
    }
  } catch (e) {
    if ($("cleanup-status")) $("cleanup-status").textContent = e.message;
  }
}

function formatDateBackfillReport(data) {
  const by = data.by_source || {};
  const lines = [
    data.would_update != null
      ? `Undated: ${(data.undated || 0).toLocaleString()}`
      : null,
    data.would_update != null
      ? `Would fill: ${(data.would_update || 0).toLocaleString()}`
      : `Updated: ${(data.updated || 0).toLocaleString()}`,
    data.unmatched != null
      ? `Still no date: ${(data.unmatched || 0).toLocaleString()}`
      : null,
    data.message || null,
    "",
    "By source:",
    ...Object.entries(by).map(([k, v]) => `  ${k}: ${Number(v).toLocaleString()}`),
  ].filter((x) => x != null);
  if (data.samples?.length) {
    lines.push("", "Samples:");
    for (const s of data.samples.slice(0, 12)) {
      lines.push(
        `  ${s.folder || "·"}/${s.filename} → ${s.inferred_date} (${s.source})`
      );
    }
  }
  return lines.join("\n");
}

$("btn-date-backfill-preview")?.addEventListener("click", async () => {
  const status = $("date-backfill-status");
  const report = $("date-backfill-report");
  try {
    if (status) status.textContent = "Previewing…";
    const data = await api("/api/dates/backfill/preview");
    if (status) {
      status.textContent = `Would fill ${(data.would_update || 0).toLocaleString()} of ${(data.undated || 0).toLocaleString()} undated photos.`;
    }
    if (report) {
      report.hidden = false;
      report.textContent = formatDateBackfillReport(data);
    }
  } catch (e) {
    if (status) status.textContent = e.message;
  }
});

$("btn-date-backfill-apply")?.addEventListener("click", async () => {
  const status = $("date-backfill-status");
  const report = $("date-backfill-report");
  if (
    !confirm(
      "Fill missing dates from folder paths and filenames?\n\n" +
        "Only photos with no date are updated. Camera EXIF is never overwritten."
    )
  ) {
    return;
  }
  try {
    if (status) status.textContent = "Filling dates…";
    const data = await api("/api/dates/backfill", {
      method: "POST",
      body: "{}",
    });
    if (status) status.textContent = data.message || `Updated ${data.updated || 0}`;
    if (report) {
      report.hidden = false;
      report.textContent = formatDateBackfillReport(data);
    }
    loadFilters();
    refreshStats();
    if (state.view === "browse") loadPhotos();
  } catch (e) {
    if (status) status.textContent = e.message;
  }
});

$("btn-cleanup-preview")?.addEventListener("click", async () => {
  const el = $("cleanup-status");
  if (el) el.textContent = "Scanning for junk / unreadable files (may take a while on SMB)…";
  try {
    const data = await api("/api/cleanup/preview", {
      method: "POST",
      body: JSON.stringify({ check_corrupt: !!$("cleanup-corrupt")?.checked }),
    });
    if (el) {
      el.textContent =
        `Preview: junk=${data.junk_count} (${formatBytes(data.junk_bytes || 0)}) · ` +
        `corrupt/unreadable=${data.corrupt_count} (${formatBytes(data.corrupt_bytes || 0)}) · ` +
        `total ${formatBytes(data.total_bytes || 0)}`;
    }
    const report = $("cleanup-report");
    if (report) {
      report.textContent = JSON.stringify(
        {
          junk_sample: data.junk_sample?.slice(0, 40),
          corrupt_sample: data.corrupt_sample?.slice(0, 40),
        },
        null,
        2
      );
    }
  } catch (e) {
    if (el) el.textContent = e.message;
  }
});

$("btn-cleanup-start")?.addEventListener("click", async () => {
  const deleteJunk = !!$("cleanup-junk")?.checked;
  const deleteCorrupt = !!$("cleanup-corrupt")?.checked;
  if (!deleteJunk && !deleteCorrupt) {
    alert("Select at least one cleanup category.");
    return;
  }
  const token = promptConfirmToken(
    CONFIRM_CLEANUP,
    "Permanently delete selected junk/corrupt files from your My Cloud drive?\n\n" +
      "This cannot be undone. Indexed photos in the library are not deleted."
  );
  if (!token) return;
  try {
    await api("/api/cleanup/start", {
      method: "POST",
      body: JSON.stringify({
        confirm: true,
        confirm_token: token,
        delete_junk: deleteJunk,
        delete_corrupt: deleteCorrupt,
        check_corrupt: deleteCorrupt,
      }),
    });
    pollCleanup();
    pollBusyJobs();
  } catch (e) {
    if ($("cleanup-status")) $("cleanup-status").textContent = e.message;
  }
});

$("btn-cleanup-cancel")?.addEventListener("click", async () => {
  try {
    await api("/api/cleanup/cancel", { method: "POST", body: "{}" });
    pollCleanup();
  } catch (e) {
    if ($("cleanup-status")) $("cleanup-status").textContent = e.message;
  }
});

/* People / faces */
let facesPoll = null;
const peopleState = {
  clustersOffset: 0,
  clustersLimit: 96,
  photosOffset: 0,
  photosLimit: 100,
  photosTotal: 0,
  photosSort: "date_desc",
  detail: null, // { type: 'cluster'|'person', key?, id?, name? }
  matches: null,
  matchSelected: new Set(),
};

function faceThumbUrl(faceId) {
  return `/api/faces/${faceId}/thumb`;
}

function formatEta(sec) {
  if (sec == null || sec < 0 || !Number.isFinite(sec)) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return `${m}m ${s}s`;
  }
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

const facesLiveSeen = new Set();

function renderFacesLiveViz(s) {
  const live = $("faces-live");
  if (!live) return;
  const running = s.status === "running";
  const paused = !!s.paused;
  const show =
    running ||
    s.status === "done" ||
    s.status === "error" ||
    s.status === "cancelled" ||
    (s.faces_found || 0) > 0 ||
    (s.processed || 0) > 0;
  live.hidden = !show;
  if (!show) return;
  live.classList.toggle("is-paused", paused);

  const pct =
    s.percent != null
      ? s.percent
      : s.total
        ? Math.min(100, Math.round((100 * (s.processed || 0)) / s.total))
        : 0;
  const pending =
    s.pending != null
      ? s.pending
      : s.total
        ? Math.max(0, s.total - (s.processed || 0))
        : 0;

  const pctEl = $("faces-live-pct");
  const phaseEl = $("faces-live-phase");
  const ring = $("faces-live-ring-fg");
  const pulse = $("faces-live-pulse");
  const pauseBtn = $("btn-faces-pause");
  const resumeBtn = $("btn-faces-resume");
  const cancelBtn = $("btn-faces-cancel");
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (phaseEl) {
    phaseEl.textContent = paused
      ? "Paused"
      : s.phase === "loading_model"
        ? "Loading"
        : s.phase === "cancelling"
          ? "Cancelling"
          : running
            ? "Scanning"
            : s.status || "";
  }
  if (ring) {
    const C = 2 * Math.PI * 52;
    const offset = C * (1 - Math.max(0, Math.min(100, pct)) / 100);
    ring.style.strokeDasharray = String(C);
    ring.style.strokeDashoffset = String(offset);
  }
  if (pulse) {
    pulse.hidden = !running;
    pulse.textContent = paused ? "Paused" : "Scanning";
  }
  if (pauseBtn) pauseBtn.hidden = !running || paused || s.phase === "cancelling";
  if (resumeBtn) resumeBtn.hidden = !running || !paused;
  if (cancelBtn) cancelBtn.hidden = !running;

  const cur = s.current_photo;
  const nowImg = $("faces-live-now-img");
  const nowPh = $("faces-live-now-placeholder");
  const nowName = $("faces-live-now-name");
  const nowLabel = $("faces-live-now-label");
  if (nowLabel) nowLabel.textContent = paused ? "Paused on" : running ? "Now scanning" : "Last photo";
  if (cur && cur.id) {
    if (nowName) nowName.textContent = cur.filename || `#${cur.id}`;
    if (nowImg) {
      const src = `/api/photos/${cur.id}/thumb`;
      if (nowImg.dataset.photoId !== String(cur.id)) {
        nowImg.dataset.photoId = String(cur.id);
        nowImg.hidden = false;
        nowImg.src = src;
      }
    }
    if (nowPh) nowPh.hidden = true;
  } else {
    if (nowName) nowName.textContent = "—";
    if (nowImg) {
      nowImg.hidden = true;
      delete nowImg.dataset.photoId;
    }
    if (nowPh) {
      nowPh.hidden = false;
      nowPh.textContent = running
        ? s.phase === "loading_model"
          ? "Loading face model…"
          : "Waiting for next photo…"
        : "Scan idle";
    }
  }

  const setStat = (id, val) => {
    const el = $(id);
    if (el) el.textContent = val;
  };
  setStat("faces-stat-pending", pending.toLocaleString());
  setStat(
    "faces-stat-processed",
    s.total
      ? `${(s.processed || 0).toLocaleString()} / ${s.total.toLocaleString()}`
      : (s.processed || 0).toLocaleString()
  );
  setStat("faces-stat-faces", (s.faces_found || 0).toLocaleString());
  setStat("faces-stat-named", (s.auto_assigned || 0).toLocaleString());
  setStat("faces-stat-errors", (s.errors || 0).toLocaleString());
  setStat("faces-stat-rate", paused ? "—" : s.images_per_sec != null ? String(s.images_per_sec) : "—");
  setStat("faces-stat-eta", paused ? "paused" : formatEta(s.eta_sec));

  const doneFill = $("faces-done-fill");
  const pendFill = $("faces-pending-fill");
  if (doneFill) doneFill.style.width = `${pct}%`;
  if (pendFill) pendFill.style.width = `${Math.max(0, 100 - pct)}%`;

  const mosaic = $("faces-live-mosaic");
  if (mosaic) {
    const recent = s.recent_faces || [];
    if (!recent.length) {
      if (!mosaic.dataset.empty) {
        mosaic.innerHTML = `<div class="faces-live-empty">${
          running
            ? s.phase === "loading_model"
              ? "Loading face model…"
              : "Watching for faces…"
            : "No faces captured in this run yet."
        }</div>`;
        mosaic.dataset.empty = "1";
      }
    } else {
      delete mosaic.dataset.empty;
      const newest = recent[0]?.id;
      if (mosaic.dataset.newest !== String(newest)) {
        mosaic.dataset.newest = String(newest || "");
        mosaic.innerHTML = recent
          .map((f) => {
            const isNew = !facesLiveSeen.has(f.id);
            facesLiveSeen.add(f.id);
            return `<div class="face-chip${f.person_id ? " named" : ""}${isNew ? " is-new" : ""}" title="face #${f.id}">
              <img src="${faceThumbUrl(f.id)}" alt="" loading="lazy"
                onerror="this.parentElement.style.opacity='0.35'" />
            </div>`;
          })
          .join("");
      }
    }
  }

  const track = $("faces-photo-film-track");
  if (track) {
    const photos = s.recent_photos || [];
    const key = photos.map((p) => p.id).join(",");
    if (track.dataset.key !== key) {
      track.dataset.key = key;
      if (!photos.length) {
        track.innerHTML = Array.from({ length: 12 }, () => `<div class="frame"></div>`).join("");
      } else {
        const frames = [...photos, ...photos]
          .map(
            (p) =>
              `<div class="frame"><img src="/api/photos/${p.id}/thumb" alt="" loading="lazy" /></div>`
          )
          .join("");
        track.innerHTML = frames;
      }
    }
  }
}

function renderFacesStatus(s) {
  const el = $("faces-status");
  const bar = $("faces-bar");
  const fill = $("faces-bar-fill");
  if (!el) return;
  const running = s.status === "running";
  updateJobProgress(bar, fill, {
    processed: s.processed,
    total: s.total,
    indeterminate: running && (!s.total || s.phase === "loading_model"),
    visible: running || s.status === "done" || s.status === "error" || s.status === "cancelled",
  });
  if (s.status === "idle" && !s.message) {
    el.textContent = "";
  } else {
    el.textContent = s.message || s.status || "";
  }
  renderFacesLiveViz(s);
}

async function pollFaceScan() {
  try {
    const s = await api("/api/faces/scan/status");
    renderFacesStatus(s);
    if (s.status === "running") {
      if (!facesPoll) facesPoll = setInterval(pollFaceScan, 1000);
    } else if (facesPoll) {
      clearInterval(facesPoll);
      facesPoll = null;
      if (state.view === "people") loadPeopleView();
    }
  } catch (_) {
    /* ignore */
  }
}

async function refreshFacesPreview() {
  const el = $("people-preview");
  if (!el) return;
  try {
    const p = await api("/api/faces/preview");
    el.textContent =
      `${(p.scanned || 0).toLocaleString()} / ${(p.images || 0).toLocaleString()} images scanned · ` +
      `${(p.pending || 0).toLocaleString()} pending · ` +
      `${(p.faces || 0).toLocaleString()} faces · ` +
      `${(p.people || 0).toLocaleString()} people · ` +
      `${(p.unnamed_clusters || 0).toLocaleString()} unnamed clusters`;
  } catch (e) {
    el.textContent = e.message;
  }
}

function renderNamedPeople(people) {
  const grid = $("people-named-grid");
  if (!grid) return;
  if (!people.length) {
    grid.innerHTML = `<div class="empty">No named people yet — scan faces, cluster, then name a group.</div>`;
    return;
  }
  grid.innerHTML = people
    .map((p) => {
      const cover = p.cover_face_id
        ? `<img src="${faceThumbUrl(p.cover_face_id)}" alt="" loading="lazy" />`
        : `<div class="empty" style="aspect-ratio:1;display:grid;place-items:center">?</div>`;
      return `<button type="button" class="person-card" data-person-id="${p.id}">
        ${cover}
        <div class="person-name">${escapeHtml(p.name)}</div>
        <div class="person-meta">${(p.photo_count || 0).toLocaleString()} photos · ${(p.face_count || 0).toLocaleString()} faces</div>
      </button>`;
    })
    .join("");
  grid.querySelectorAll("[data-person-id]").forEach((btn) => {
    btn.addEventListener("click", () => openPersonDetail(Number(btn.dataset.personId)));
  });
}

function renderClusters(data) {
  const grid = $("people-clusters-grid");
  const pager = $("people-clusters-pager");
  const label = $("clusters-pager-label");
  if (!grid) return;
  const clusters = data.clusters || [];
  if (!clusters.length) {
    grid.innerHTML = `<div class="empty">No unnamed clusters. Run Cluster unknowns after a face scan.</div>`;
    if (pager) pager.hidden = true;
    return;
  }
  grid.innerHTML = clusters
    .map((c) => {
      const sample = (c.sample_faces || [])[0];
      const img = sample
        ? `<img src="${faceThumbUrl(sample.id)}" alt="" loading="lazy" />`
        : `<div class="empty" style="aspect-ratio:1"></div>`;
      return `<button type="button" class="person-card" data-cluster-key="${escapeHtml(c.cluster_key)}">
        ${img}
        <div class="person-name">Unnamed</div>
        <div class="person-meta">${(c.photo_count || 0).toLocaleString()} photos · ${(c.face_count || 0).toLocaleString()} faces</div>
      </button>`;
    })
    .join("");
  grid.querySelectorAll("[data-cluster-key]").forEach((btn) => {
    btn.addEventListener("click", () => openClusterDetail(btn.dataset.clusterKey));
  });
  const total = data.total || 0;
  const offset = data.offset || 0;
  const limit = data.limit || peopleState.clustersLimit;
  if (pager) pager.hidden = total <= limit;
  if (label) {
    label.textContent = `${offset + 1}–${Math.min(offset + clusters.length, total)} of ${total}`;
  }
  const prev = $("btn-clusters-prev");
  const next = $("btn-clusters-next");
  if (prev) prev.disabled = offset <= 0;
  if (next) next.disabled = offset + limit >= total;
}

function showPeopleListPanels(show) {
  const named = $("people-named-panel");
  const clusters = $("people-clusters-panel");
  const detail = $("people-detail-panel");
  const pager = $("people-detail-pager");
  const toolbar = $("people-photos-toolbar");
  if (named) named.hidden = !show;
  if (clusters) clusters.hidden = !show;
  if (detail) detail.hidden = show;
  if (show && pager) pager.hidden = true;
  if (show && toolbar) toolbar.hidden = true;
}

async function loadPeopleView() {
  showPeopleListPanels(true);
  peopleState.detail = null;
  const matchesPanel = $("people-matches-panel");
  if (matchesPanel) matchesPanel.hidden = true;
  const grid = $("people-clusters-grid");
  if (grid && !grid.childElementCount) {
    grid.innerHTML = `<div class="empty">Loading unnamed groups…</div>`;
  }
  await refreshFacesPreview();
  pollFaceScan();
  try {
    const [peopleData, clustersData] = await Promise.all([
      api("/api/people"),
      api(
        `/api/faces/clusters?limit=${peopleState.clustersLimit}&offset=${peopleState.clustersOffset}`
      ),
    ]);
    renderNamedPeople(peopleData.people || []);
    renderClusters(clustersData);
  } catch (e) {
    if ($("faces-status")) $("faces-status").textContent = e.message;
    if (grid) grid.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

function renderFaceStrip(faces) {
  const strip = $("people-detail-faces");
  if (!strip) return;
  strip.innerHTML = (faces || [])
    .map(
      (f) =>
        `<div class="face-chip"><img src="${faceThumbUrl(f.id)}" alt="" loading="lazy" /></div>`
    )
    .join("");
}

function updatePeoplePhotosPager() {
  const pager = $("people-detail-pager");
  const label = $("people-photos-pager-label");
  const prev = $("btn-people-photos-prev");
  const next = $("btn-people-photos-next");
  const total = peopleState.photosTotal || 0;
  const offset = peopleState.photosOffset || 0;
  const limit = peopleState.photosLimit || 100;
  const shown = Math.min(limit, Math.max(0, total - offset));
  if (pager) pager.hidden = total <= limit && offset === 0;
  if (label) {
    label.textContent = total
      ? `${offset + 1}–${offset + shown} of ${total.toLocaleString()}`
      : "No photos";
  }
  if (prev) prev.disabled = offset <= 0;
  if (next) next.disabled = offset + limit >= total;
}

function renderPersonPhotoGrid(items) {
  const grid = $("people-detail-photos");
  if (!grid) return;
  if (!items.length) {
    grid.innerHTML = `<div class="empty">No photos</div>`;
    updatePeoplePhotosPager();
    return;
  }
  grid.innerHTML = "";
  for (const p of items) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <img loading="lazy" src="/api/photos/${p.id}/thumb" alt=""
        onerror="this.style.background='#1e2a38';this.removeAttribute('src')" />
      <div class="cap">${escapeHtml(p.filename || "")}</div>
    `;
    card.addEventListener("click", () => openLightbox(p));
    grid.appendChild(card);
  }
  updatePeoplePhotosPager();
}

async function fillPeopleNameHelpers(people, { excludeId = null } = {}) {
  const list = (people || []).filter((p) => p.id !== excludeId);
  const datalist = $("people-name-suggestions");
  if (datalist) {
    datalist.innerHTML = list
      .map((p) => `<option value="${escapeHtml(p.name)}"></option>`)
      .join("");
  }
  const existingRow = $("people-existing-row");
  const existingSel = $("people-existing-select");
  const assignBtn = $("btn-people-assign-existing");
  if (existingRow && existingSel && assignBtn) {
    if (list.length) {
      existingRow.hidden = false;
      assignBtn.hidden = false;
      existingSel.innerHTML =
        `<option value="">Select a named person…</option>` +
        list
          .map(
            (p) =>
              `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} · ${(p.photo_count || 0).toLocaleString()} photos</option>`
          )
          .join("");
    } else {
      existingRow.hidden = true;
      assignBtn.hidden = true;
      existingSel.innerHTML = `<option value="">Select a named person…</option>`;
    }
  }
}

async function openClusterDetail(clusterKey) {
  try {
    const [data, peopleData] = await Promise.all([
      api(`/api/faces/clusters/${encodeURIComponent(clusterKey)}`),
      api("/api/people"),
    ]);
    peopleState.detail = { type: "cluster", key: clusterKey };
    peopleState.matches = null;
    showPeopleListPanels(false);
    const matchesPanel = $("people-matches-panel");
    if (matchesPanel) matchesPanel.hidden = true;
    const title = $("people-detail-title");
    if (title) {
      title.textContent = `Unnamed · ${(data.photo_count || 0).toLocaleString()} photos · ${(data.face_count || 0).toLocaleString()} faces`;
    }
    renderFaceStrip(data.faces || []);
    const nameRow = $("people-name-row");
    const nameActions = $("people-name-actions");
    const mergeSel = $("people-merge-select");
    const mergeBtn = $("btn-people-merge-into");
    const findBtn = $("btn-people-find-matches");
    if (nameRow) nameRow.hidden = false;
    if (nameActions) nameActions.hidden = false;
    if ($("people-name-input")) $("people-name-input").value = "";
    if (mergeSel) mergeSel.hidden = true;
    if (mergeBtn) mergeBtn.hidden = true;
    if (findBtn) findBtn.hidden = true;
    const ignoreBtn = $("btn-people-ignore-cluster");
    if (ignoreBtn) ignoreBtn.hidden = false;
    await fillPeopleNameHelpers(peopleData.people || []);
    // Show photo thumbs via first faces' photo ids uniquely
    const photoIds = [...new Set((data.faces || []).map((f) => f.photo_id))];
    const items = photoIds.slice(0, 48).map((id) => ({ id, filename: `#${id}` }));
    peopleState.photosTotal = photoIds.length;
    peopleState.photosOffset = 0;
    renderPersonPhotoGrid(items);
    const pager = $("people-detail-pager");
    if (pager) pager.hidden = true;
    const toolbar = $("people-photos-toolbar");
    if (toolbar) toolbar.hidden = true;
  } catch (e) {
    alert(e.message);
  }
}

async function loadPersonPhotosPage() {
  const d = peopleState.detail;
  if (!d || d.type !== "person" || d.id == null) return;
  const limit = peopleState.photosLimit || 100;
  const offset = peopleState.photosOffset || 0;
  const sort =
    $("people-sort-filter")?.value || peopleState.photosSort || "date_desc";
  peopleState.photosSort = sort;
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    sort,
  });
  const data = await api(`/api/people/${d.id}/photos?${params}`);
  peopleState.photosTotal = data.total || 0;
  peopleState.detail = { ...d, name: data.name };
  const title = $("people-detail-title");
  if (title) {
    title.textContent = `${data.name} · ${(data.total || 0).toLocaleString()} photos`;
  }
  const toolbar = $("people-photos-toolbar");
  if (toolbar) toolbar.hidden = false;
  const sortEl = $("people-sort-filter");
  if (sortEl) sortEl.value = sort;
  renderPersonPhotoGrid(data.items || []);
  return data;
}

async function openPersonDetail(personId, { resetPage = true } = {}) {
  try {
    if (resetPage) peopleState.photosOffset = 0;
    peopleState.detail = { type: "person", id: personId };
    peopleState.matches = null;
    showPeopleListPanels(false);
    const matchesPanel = $("people-matches-panel");
    if (matchesPanel) matchesPanel.hidden = true;
    const data = await loadPersonPhotosPage();
    renderFaceStrip([]);
    const nameRow = $("people-name-row");
    const nameActions = $("people-name-actions");
    const existingRow = $("people-existing-row");
    const assignBtn = $("btn-people-assign-existing");
    const findBtn = $("btn-people-find-matches");
    if (nameRow) nameRow.hidden = false;
    if (nameActions) nameActions.hidden = false;
    if (existingRow) existingRow.hidden = true;
    if (assignBtn) assignBtn.hidden = true;
    if (findBtn) findBtn.hidden = false;
    if ($("people-name-input")) $("people-name-input").value = data?.name || "";
    const ignoreBtn = $("btn-people-ignore-cluster");
    if (ignoreBtn) ignoreBtn.hidden = true;
    const mergeSel = $("people-merge-select");
    const mergeBtn = $("btn-people-merge-into");
    const peopleData = await api("/api/people");
    const others = (peopleData.people || []).filter((p) => p.id !== personId);
    const datalist = $("people-name-suggestions");
    if (datalist) datalist.innerHTML = "";
    if (mergeSel && mergeBtn) {
      if (others.length) {
        mergeSel.hidden = false;
        mergeBtn.hidden = false;
        mergeSel.innerHTML =
          `<option value="">Merge into…</option>` +
          others
            .map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`)
            .join("");
      } else {
        mergeSel.hidden = true;
        mergeBtn.hidden = true;
      }
    }
  } catch (e) {
    alert(e.message);
  }
}

function renderPersonMatches(data) {
  const panel = $("people-matches-panel");
  const grid = $("people-matches-grid");
  const status = $("people-matches-status");
  if (!panel || !grid) return;
  peopleState.matches = data;
  peopleState.matchSelected = new Set(
    (data.matches || []).filter((m) => m.auto_suggest).map((m) => m.id)
  );
  panel.hidden = false;
  if (status) status.textContent = data.message || "";
  const matches = data.matches || [];
  if (!matches.length) {
    grid.innerHTML = `<div class="empty">No close matches among unnamed faces yet. Name more samples or wait for more face scanning.</div>`;
    return;
  }
  grid.innerHTML = matches
    .map((m) => {
      const selected = peopleState.matchSelected.has(m.id);
      return `<button type="button" class="people-match-card${selected ? " selected" : ""}${
        m.auto_suggest ? " auto-suggest" : ""
      }" data-face-id="${m.id}" title="distance ${m.distance}">
        <span class="match-check" aria-hidden="true"></span>
        <img src="${faceThumbUrl(m.id)}" alt="" loading="lazy" />
        <div class="match-meta">${m.auto_suggest ? "likely · " : ""}${m.distance}</div>
      </button>`;
    })
    .join("");
  grid.querySelectorAll("[data-face-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.faceId);
      if (peopleState.matchSelected.has(id)) peopleState.matchSelected.delete(id);
      else peopleState.matchSelected.add(id);
      btn.classList.toggle("selected", peopleState.matchSelected.has(id));
    });
  });
}

$("btn-faces-preview")?.addEventListener("click", () => refreshFacesPreview());

$("btn-faces-scan")?.addEventListener("click", async () => {
  if (
    !confirm(
      "Scan faces in all library images?\n\nThis can take hours on a large library and downloads a local model on first run. You can cancel and resume later."
    )
  ) {
    return;
  }
  try {
    facesLiveSeen.clear();
    const mosaic = $("faces-live-mosaic");
    if (mosaic) {
      mosaic.innerHTML = "";
      delete mosaic.dataset.newest;
      delete mosaic.dataset.empty;
    }
    const s = await api("/api/faces/scan/start", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    renderFacesStatus(s);
    pollFaceScan();
    pollBusyJobs();
  } catch (e) {
    if ($("faces-status")) $("faces-status").textContent = e.message;
  }
});

$("btn-faces-cluster")?.addEventListener("click", async () => {
  const el = $("faces-status");
  const grid = $("people-clusters-grid");
  if (grid) grid.innerHTML = `<div class="empty">Loading unnamed groups…</div>`;
  try {
    const preview = await api("/api/faces/preview");
    const already = preview.unnamed_clusters || 0;
    if (already > 0) {
      if (el) {
        el.textContent = `${already.toLocaleString()} unnamed groups ready — loading…`;
      }
      peopleState.clustersOffset = 0;
      showPeopleListPanels(true);
      await loadPeopleView();
      $("people-clusters-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
      if (el) {
        el.textContent = `${already.toLocaleString()} unnamed groups ready to name`;
      }
      return;
    }
    if (el) el.textContent = "Clustering unnamed faces (can take a couple minutes)…";
    const r = await api("/api/faces/cluster", { method: "POST", body: "{}" });
    if (el) el.textContent = r.message || "Clustering done";
    peopleState.clustersOffset = 0;
    showPeopleListPanels(true);
    await loadPeopleView();
    $("people-clusters-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    if (el) el.textContent = e.message;
    if (grid) grid.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
});

$("btn-faces-cancel")?.addEventListener("click", async () => {
  await requestJobCancel("/api/faces/scan/cancel", "Face scan");
  pollFaceScan();
});

$("btn-faces-pause")?.addEventListener("click", async () => {
  try {
    const s = await api("/api/faces/scan/pause", { method: "POST", body: "{}" });
    renderFacesStatus(s);
    pollBusyJobs();
  } catch (e) {
    if ($("faces-status")) $("faces-status").textContent = e.message;
  }
});

$("btn-faces-resume")?.addEventListener("click", async () => {
  try {
    const s = await api("/api/faces/scan/resume", { method: "POST", body: "{}" });
    renderFacesStatus(s);
    pollBusyJobs();
  } catch (e) {
    if ($("faces-status")) $("faces-status").textContent = e.message;
  }
});

$("btn-clusters-prev")?.addEventListener("click", async () => {
  peopleState.clustersOffset = Math.max(
    0,
    peopleState.clustersOffset - peopleState.clustersLimit
  );
  await loadPeopleView();
});

$("btn-clusters-next")?.addEventListener("click", async () => {
  peopleState.clustersOffset += peopleState.clustersLimit;
  await loadPeopleView();
});

$("btn-people-back")?.addEventListener("click", () => loadPeopleView());

$("btn-people-photos-prev")?.addEventListener("click", async () => {
  if (!peopleState.detail || peopleState.detail.type !== "person") return;
  peopleState.photosOffset = Math.max(
    0,
    (peopleState.photosOffset || 0) - (peopleState.photosLimit || 100)
  );
  try {
    await loadPersonPhotosPage();
    $("people-detail-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    alert(e.message);
  }
});

$("btn-people-photos-next")?.addEventListener("click", async () => {
  if (!peopleState.detail || peopleState.detail.type !== "person") return;
  peopleState.photosOffset =
    (peopleState.photosOffset || 0) + (peopleState.photosLimit || 100);
  try {
    await loadPersonPhotosPage();
    $("people-detail-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    alert(e.message);
  }
});

$("people-sort-filter")?.addEventListener("change", async () => {
  if (!peopleState.detail || peopleState.detail.type !== "person") return;
  peopleState.photosSort = $("people-sort-filter").value || "date_desc";
  peopleState.photosOffset = 0;
  try {
    await loadPersonPhotosPage();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-people-name")?.addEventListener("click", async () => {
  const name = ($("people-name-input")?.value || "").trim();
  if (!name) {
    alert("Enter a name");
    return;
  }
  const d = peopleState.detail;
  if (!d) return;
  try {
    if (d.type === "cluster") {
      await api("/api/faces/clusters/name", {
        method: "POST",
        body: JSON.stringify({ cluster_key: d.key, name }),
      });
    } else if (d.type === "person") {
      await api(`/api/people/${d.id}/rename`, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
    }
    await loadPeopleView();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-people-assign-existing")?.addEventListener("click", async () => {
  const d = peopleState.detail;
  if (!d || d.type !== "cluster") return;
  const name = ($("people-existing-select")?.value || "").trim();
  if (!name) {
    alert("Select an existing person first");
    return;
  }
  try {
    await api("/api/faces/clusters/name", {
      method: "POST",
      body: JSON.stringify({ cluster_key: d.key, name }),
    });
    await loadPeopleView();
  } catch (e) {
    alert(e.message);
  }
});

$("people-existing-select")?.addEventListener("change", () => {
  const name = ($("people-existing-select")?.value || "").trim();
  const input = $("people-name-input");
  if (name && input) input.value = name;
});

$("btn-people-find-matches")?.addEventListener("click", async () => {
  const d = peopleState.detail;
  if (!d || d.type !== "person") return;
  const status = $("people-matches-status");
  const panel = $("people-matches-panel");
  if (panel) panel.hidden = false;
  if (status) status.textContent = "Searching unnamed faces…";
  try {
    const data = await api(
      `/api/people/${d.id}/find-matches?limit=120`,
      { method: "POST", body: "{}" }
    );
    renderPersonMatches(data);
  } catch (e) {
    if (status) status.textContent = e.message;
    alert(e.message);
  }
});

$("btn-people-matches-select-auto")?.addEventListener("click", () => {
  const matches = peopleState.matches?.matches || [];
  peopleState.matchSelected = new Set(
    matches.filter((m) => m.auto_suggest).map((m) => m.id)
  );
  document.querySelectorAll("#people-matches-grid [data-face-id]").forEach((btn) => {
    const id = Number(btn.dataset.faceId);
    btn.classList.toggle("selected", peopleState.matchSelected.has(id));
  });
});

$("btn-people-matches-select-all")?.addEventListener("click", () => {
  const matches = peopleState.matches?.matches || [];
  peopleState.matchSelected = new Set(matches.map((m) => m.id));
  document.querySelectorAll("#people-matches-grid [data-face-id]").forEach((btn) => {
    btn.classList.add("selected");
  });
});

$("btn-people-matches-clear")?.addEventListener("click", () => {
  peopleState.matchSelected = new Set();
  document.querySelectorAll("#people-matches-grid [data-face-id]").forEach((btn) => {
    btn.classList.remove("selected");
  });
});

$("btn-people-matches-accept")?.addEventListener("click", async () => {
  const d = peopleState.detail;
  if (!d || d.type !== "person") return;
  const ids = [...peopleState.matchSelected];
  if (!ids.length) {
    alert("Select at least one match");
    return;
  }
  try {
    const r = await api(`/api/people/${d.id}/accept-matches`, {
      method: "POST",
      body: JSON.stringify({ face_ids: ids }),
    });
    alert(r.message || `Added ${r.assigned} faces`);
    await openPersonDetail(d.id);
  } catch (e) {
    alert(e.message);
  }
});

$("btn-people-ignore-cluster")?.addEventListener("click", async () => {
  const d = peopleState.detail;
  if (!d || d.type !== "cluster") return;
  if (!confirm("Mark this group as not a person? Faces will be hidden from naming.")) return;
  try {
    await api("/api/faces/clusters/ignore", {
      method: "POST",
      body: JSON.stringify({ cluster_key: d.key }),
    });
    await loadPeopleView();
  } catch (e) {
    alert(e.message);
  }
});

$("btn-people-merge-into")?.addEventListener("click", async () => {
  const d = peopleState.detail;
  if (!d || d.type !== "person") return;
  const targetId = Number($("people-merge-select")?.value || 0);
  if (!targetId) {
    alert("Pick a person to merge into");
    return;
  }
  if (!confirm(`Merge “${d.name}” into the selected person? This cannot be undone easily.`)) {
    return;
  }
  try {
    await api("/api/people/merge", {
      method: "POST",
      body: JSON.stringify({ source_id: d.id, target_id: targetId }),
    });
    await loadPeopleView();
  } catch (e) {
    alert(e.message);
  }
});

/* Boot */
refreshStats();
loadFilters();
loadPhotos();
pollScan();
loadSettings();
loadExactBatchPreview();
pollExactBatch();
pollOrganize();
pollImport();
pollCleanup();
pollArchive();
pollInventory();
pollRawConvert();
pollFaceScan();
if (typeof initArchiveDisclosurePrefs === "function") initArchiveDisclosurePrefs();
if (typeof refreshIcloudStatus === "function") refreshIcloudStatus();
pollBusyJobs();
if (!state.busyPoll) state.busyPoll = setInterval(pollBusyJobs, 4000);
