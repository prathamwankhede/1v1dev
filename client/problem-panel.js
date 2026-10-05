/* ═══════════════════════════════════════════════════
   1v1dev — Problem sidebar: Problem · Rules Ref · Test Cases · Submissions
   Owns the attempt history the case badges and the Submissions tab are
   built from.
   ═══════════════════════════════════════════════════ */

import {
  languageSelect, verdictPanel, problemTitle, problemDescription,
  testCasesContainer,
} from './dom.js';
import { escapeHtml, formatTime } from './util.js';
import { state } from './state.js';

const tabButtons = [...document.querySelectorAll('[data-problem-tab]')];
const tabPanels = [...document.querySelectorAll('[data-problem-panel]')];
const rulesTabBtn = document.querySelector('[data-problem-tab="rules"]');
const problemMeta = document.getElementById('problem-meta');
const constraintEl = document.getElementById('problem-constraint');
const inputSpecEl = document.getElementById('problem-input-spec');
const importErrorEls = [...document.querySelectorAll('.import-error-slot')];
const caseCountEl = document.getElementById('case-count');
const fullStatement = document.getElementById('problem-full');
const fullStatementText = document.getElementById('problem-full-text');
const rulesRef = document.getElementById('rules-ref');
const allCasesEl = document.getElementById('all-cases');
const hiddenSummaryEl = document.getElementById('hidden-summary');
const submissionsList = document.getElementById('submissions-list');
const submissionsCount = document.getElementById('submissions-count');

const DIFFICULTY_LABELS = { easy: 'Easy', medium: 'Medium', hard: 'Hard' };
const LANGUAGE_LABELS = { python: 'Python 3', javascript: 'JavaScript' };
const FIELD_TONES = ['tone-sky', 'tone-green', 'tone-amber'];

// Attempt summaries, oldest first, in the shape of submissionResult /
// resumeState.attempts. An attempt still being judged is {attempt, judged:false}.
let attempts = [];
let selectedAttempt = null;   // attempt number shown in #verdict-panel

// ── Tabs ────────────────────────────────────────────
function selectTab(name) {
  tabButtons.forEach((btn) => {
    const on = btn.dataset.problemTab === name;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', String(on));
    btn.tabIndex = on ? 0 : -1;
  });
  tabPanels.forEach((panel) => {
    panel.hidden = panel.dataset.problemPanel !== name;
  });
}

tabButtons.forEach((btn) => {
  btn.addEventListener('click', () => selectTab(btn.dataset.problemTab));
  // Arrow keys move between visible tabs, per the WAI-ARIA tabs pattern.
  btn.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const visible = tabButtons.filter((b) => !b.hidden);
    const i = visible.indexOf(btn) + (e.key === 'ArrowRight' ? 1 : -1);
    const next = visible[(i + visible.length) % visible.length];
    selectTab(next.dataset.problemTab);
    next.focus();
  });
});

// ── Problem tab ─────────────────────────────────────
// From the problem payload, not editor.js's `bundles`: the problem renders
// before the editor builds its docs, and both hold the same normalized shape.
function currentBundle(prob) {
  return ((prob && prob.files) || {})[languageSelect.value] || { files: [] };
}

// Escape first, then turn `path` spans into chips. The escaping is what
// keeps problem text from ever injecting markup (Phase 4 security note).
function withCodeChips(text, bundle) {
  const locked = new Set(bundle.files.filter((f) => !f.writable).map((f) => f.path));
  const writable = new Set(bundle.files.filter((f) => f.writable).map((f) => f.path));
  return escapeHtml(text).replace(/`([^`]+)`/g, (_, code) => {
    const tone = locked.has(code) ? ' chip-locked' : writable.has(code) ? ' chip-writable' : '';
    return `<code class="code-chip${tone}">${code}</code>`;
  });
}

function chip(path, tone) {
  return `<code class="code-chip ${tone}">${escapeHtml(path)}</code>`;
}

function joinList(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

// Derived from the bundle, not a problem field: shown whenever a file is
// locked, naming which files are read-only and which ones get submitted.
function renderConstraint(bundle) {
  const locked = bundle.files.filter((f) => !f.writable).map((f) => chip(f.path, 'chip-locked'));
  const writable = bundle.files.filter((f) => f.writable).map((f) => chip(f.path, 'chip-writable'));
  if (!locked.length) {
    constraintEl.hidden = true;
    constraintEl.innerHTML = '';
    return;
  }
  constraintEl.hidden = false;
  constraintEl.innerHTML = `
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
    </svg>
    <div><strong>File Constraint:</strong> ${joinList(locked)} ${locked.length === 1 ? 'is' : 'are'}
    locked and correct — read ${locked.length === 1 ? 'it' : 'them'} for the rules. Fix and submit
    ${joinList(writable)}.</div>`;
}

function renderInputSpec(spec) {
  if (!spec) {
    inputSpecEl.hidden = true;
    inputSpecEl.innerHTML = '';
    return;
  }
  const fields = (spec.fields || []).map((f, i) =>
    `<span class="field-chip ${FIELD_TONES[i % FIELD_TONES.length]}">${escapeHtml(f)}</span>`).join('');
  inputSpecEl.hidden = false;
  inputSpecEl.innerHTML = `
    <h2 class="ui-label">Input Specification</h2>
    <div class="spec-card">
      ${spec.summary ? `<p>${escapeHtml(spec.summary)}</p>` : ''}
      ${fields ? `<div class="field-chips">${fields}</div>` : ''}
      ${spec.note ? `<p class="spec-note">${escapeHtml(spec.note)}</p>` : ''}
    </div>`;
}

export function renderProblem(prob) {
  const bundle = currentBundle(prob);

  const meta = [];
  if (prob.difficulty) {
    meta.push(`<span class="difficulty-chip ${prob.difficulty}">${DIFFICULTY_LABELS[prob.difficulty]}</span>`);
  }
  if (prob.category) meta.push(`<span>${escapeHtml(prob.category)}</span>`);
  if (meta.length) meta.push(`<span>${LANGUAGE_LABELS[languageSelect.value] || ''}</span>`);
  problemMeta.hidden = meta.length === 0;
  problemMeta.innerHTML = meta.join('<span class="crumb-sep">/</span>');

  problemTitle.textContent = prob.title;

  // With a summary, show it plus the input spec, and keep the complete
  // statement one click away — it still carries the output format and the
  // worked example. Without one, show the statement as before.
  if (prob.summary) {
    problemDescription.classList.remove('as-statement');
    problemDescription.innerHTML = withCodeChips(prob.summary, bundle);
    fullStatement.hidden = false;
    fullStatementText.textContent = prob.description;
  } else {
    problemDescription.classList.add('as-statement');
    problemDescription.textContent = prob.description;
    fullStatement.hidden = true;
    fullStatementText.textContent = '';
  }

  renderConstraint(bundle);
  renderInputSpec(prob.inputSpec);
  renderRulesRef(bundle);
  renderCases();
}

// ── Case cards ──────────────────────────────────────
function latestJudged() {
  for (let i = attempts.length - 1; i >= 0; i--) {
    if (attempts[i].judged !== false) return attempts[i];
  }
  return null;
}

function isJudging() {
  const last = attempts[attempts.length - 1];
  return !!last && last.judged === false;
}

// Sample card k matches the k-th non-hidden result: raceStart.testCases
// holds only samples, and _public_results keeps source order. Matching
// on `index` would be wrong — a sample's position in the full suite is
// not known client-side.
function caseStatuses() {
  const samples = (state.problem && state.problem.testCases) || [];
  if (isJudging()) return samples.map(() => ({ status: 'judging' }));
  const latest = latestJudged();
  if (!latest) return samples.map(() => ({ status: 'not-run' }));
  const visible = (latest.results || []).filter((r) => !r.hidden);
  return samples.map((_, k) => {
    const r = visible[k];
    if (!r) return { status: 'not-run' };
    return { status: r.passed ? 'passed' : 'failed', actual: r.actual, error: r.error };
  });
}

const BADGE_LABELS = { 'not-run': 'Not Run', judging: 'Judging', passed: 'Passed', failed: 'Failed' };

function caseCard(tc, k, st) {
  const failed = st.status === 'failed';
  const actual = failed && st.actual !== undefined && st.actual !== null
    ? `<div class="case-wide"><span class="case-label">Your Output</span><pre class="case-io actual">${escapeHtml(st.actual)}</pre></div>`
    : '';
  const error = failed && st.error
    ? `<pre class="case-error">${escapeHtml(st.error)}</pre>`
    : '';
  return `<div class="case-card">
    <div class="case-head">
      <span class="case-name">Case ${k + 1}</span>
      <span class="case-badge ${st.status}">${BADGE_LABELS[st.status]}</span>
      <button class="case-copy" type="button" data-case="${k}" aria-label="Copy input for case ${k + 1}">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/>
        </svg>
        <span>copy</span>
      </button>
    </div>
    <div class="case-grid">
      <div><span class="case-label">Input</span><pre class="case-io">${escapeHtml(tc.input)}</pre></div>
      <div><span class="case-label">Expected Output</span><pre class="case-io expected">${escapeHtml(tc.expectedOutput)}</pre></div>
      ${actual}
    </div>
    ${error}
  </div>`;
}

function renderImportError(latest) {
  const message = latest && latest.importError;
  importErrorEls.forEach((el) => {
    el.hidden = !message;
    el.innerHTML = message
      ? `<div class="verdict-import-error">Your code failed to import, so every test failed the same way:\n${escapeHtml(message)}</div>`
      : '';
  });
}

function renderCases() {
  const samples = (state.problem && state.problem.testCases) || [];
  const statuses = caseStatuses();
  const cards = samples.map((tc, k) => caseCard(tc, k, statuses[k])).join('');
  testCasesContainer.innerHTML = cards;
  allCasesEl.innerHTML = cards;
  caseCountEl.textContent = `${samples.length} ${samples.length === 1 ? 'case' : 'cases'}`;

  const latest = isJudging() ? null : latestJudged();
  renderImportError(latest);

  const totalHidden = state.problem
    ? Math.max(0, (state.problem.totalTests || 0) - samples.length)
    : 0;
  if (!totalHidden) {
    hiddenSummaryEl.hidden = true;
    return;
  }
  hiddenSummaryEl.hidden = false;
  const hidden = latest ? (latest.results || []).filter((r) => r.hidden) : [];
  hiddenSummaryEl.textContent = isJudging()
    ? `Hidden tests: ${totalHidden} — judging…`
    : hidden.length
      ? `Hidden tests: ${hidden.filter((r) => r.passed).length}/${hidden.length} passed`
      : `Hidden tests: ${totalHidden} — not run yet`;
}

// Copy a case's input. The Clipboard API needs a secure context: localhost
// qualifies, a plain-HTTP LAN address does not — then select the text so
// the player can copy it by hand.
function onCopyClick(e) {
  const btn = e.target.closest('.case-copy');
  if (!btn) return;
  const tc = state.problem.testCases[Number(btn.dataset.case)];
  const label = btn.querySelector('span');
  const done = (text) => {
    label.textContent = text;
    setTimeout(() => { label.textContent = 'copy'; }, 1200);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(tc.input).then(() => done('copied'), () => done('failed'));
    return;
  }
  const pre = btn.closest('.case-card').querySelector('.case-io');
  const range = document.createRange();
  range.selectNodeContents(pre);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  done('selected');
}

testCasesContainer.addEventListener('click', onCopyClick);
allCasesEl.addEventListener('click', onCopyClick);

// ── Rules Ref tab ───────────────────────────────────
// Read-only, highlighted copies of the locked files. The tab is hidden
// when there are none, which covers every legacy single-file problem.
function renderRulesRef(bundle) {
  const locked = bundle.files.filter((f) => !f.writable);
  rulesTabBtn.hidden = locked.length === 0;
  if (!locked.length && rulesTabBtn.classList.contains('active')) selectTab('problem');

  const mode = languageSelect.value === 'javascript' ? 'javascript' : 'python';
  rulesRef.innerHTML = '';
  locked.forEach((f) => {
    const section = document.createElement('section');
    section.className = 'rules-file';
    section.innerHTML = `<div class="rules-file-head">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/>
      </svg>${escapeHtml(f.path)}<span class="rules-file-tag">read-only</span></div>`;
    const pre = document.createElement('pre');
    pre.className = 'rules-code cm-s-1v1dev';
    CodeMirror.runMode(f.content, mode, pre);
    section.appendChild(pre);
    rulesRef.appendChild(section);
  });
}

// ── Submissions tab ─────────────────────────────────
function attemptLine(a) {
  if (a.judged === false) return 'Judging…';
  const outcome = a.accepted ? 'Accepted' : 'Rejected';
  return `${outcome} · ${a.passCount}/${a.totalTests}`;
}

function renderSubmissions() {
  submissionsCount.textContent = attempts.length ? String(attempts.length) : '';
  if (!attempts.length) {
    submissionsList.innerHTML = '<li class="submissions-empty">No attempts yet. Submit to run every test case.</li>';
    hideVerdict();
    return;
  }
  submissionsList.innerHTML = attempts.slice().reverse().map((a) => {
    const tone = a.judged === false ? 'pending' : a.accepted ? 'accepted' : 'rejected';
    const time = a.submittedAtMs !== undefined ? formatTime(a.submittedAtMs) : '';
    const flag = a.importError ? '<span class="attempt-flag">import error</span>' : '';
    return `<li><button class="attempt-row ${tone}${a.attempt === selectedAttempt ? ' selected' : ''}" type="button" data-attempt="${a.attempt}">
      <span class="attempt-num">#${a.attempt}</span>
      <span class="attempt-time">${time}</span>
      <span class="attempt-outcome">${attemptLine(a)}</span>${flag}
    </button></li>`;
  }).join('');

  const shown = attempts.find((a) => a.attempt === selectedAttempt);
  if (shown && shown.judged !== false) renderVerdict(shown);
  else hideVerdict();
}

submissionsList.addEventListener('click', (e) => {
  const row = e.target.closest('.attempt-row');
  if (!row) return;
  selectedAttempt = Number(row.dataset.attempt);
  renderSubmissions();
});

// ── Verdicts ────────────────────────────────────────
// A submission has to pass every test case to be accepted. Anything less
// comes back rejected and the player can fix it and submit again.
export function renderVerdict(data) {
  const { accepted, passCount, totalTests, results, attempt, importError } = data;
  verdictPanel.style.display = '';
  verdictPanel.className = `verdict-panel ${accepted ? 'accepted' : 'rejected'}`;

  const heading = accepted
    ? `Accepted — ${passCount}/${totalTests} tests passed`
    : `Rejected — ${passCount}/${totalTests} tests passed`;

  // Every case fails identically when a writable file fails to import —
  // call that out distinctly instead of N identical wrong-answer rows.
  const importBanner = importError
    ? `<div class="verdict-import-error">Your code failed to import:\n${escapeHtml(importError)}</div>`
    : '';

  const rows = (results || []).map((r) => {
    const mark = r.passed ? '✓' : '✗';
    const cls = r.passed ? 'pass' : 'fail';
    const name = r.hidden ? `Hidden test ${r.index}` : `Test ${r.index}`;
    let detail = '';
    // Hidden cases never carry input/expected, so there is nothing to show
    // beyond the pass mark and any crash message.
    if (!r.passed && !r.hidden) {
      detail =
        `<pre class="verdict-diff">` +
        `input:    ${escapeHtml(r.input || '')}\n` +
        `expected: ${escapeHtml(r.expected || '')}\n` +
        `actual:   ${escapeHtml(r.actual || '')}</pre>`;
    } else if (!r.passed && r.error) {
      detail = `<pre class="verdict-diff">${escapeHtml(r.error)}</pre>`;
    }
    return `<li class="verdict-row ${cls}"><span class="verdict-mark">${mark}</span>` +
           `<span>${name}</span>${detail}</li>`;
  }).join('');

  verdictPanel.innerHTML =
    `<div class="verdict-heading">Attempt ${attempt} — ${escapeHtml(heading)}</div>` +
    importBanner +
    `<ul class="verdict-list">${rows}</ul>`;
}

export function hideVerdict() {
  verdictPanel.style.display = 'none';
  verdictPanel.innerHTML = '';
}

// ── Attempt history (driven by app.js / resume.js) ───
function upsert(summary) {
  const i = attempts.findIndex((a) => a.attempt === summary.attempt);
  if (i === -1) attempts.push(summary);
  else attempts[i] = summary;
  selectedAttempt = summary.attempt;
  renderSubmissions();
  renderCases();
}

// 'judging': attempt N is queued.
export function markJudging(attemptNumber) {
  upsert({ attempt: attemptNumber, judged: false });
}

// 'submissionResult'.
export function recordAttempt(data) {
  upsert({
    attempt: data.attempt,
    judged: true,
    accepted: !!data.accepted,
    passCount: data.passCount,
    totalTests: data.totalTests,
    results: data.results || [],
    importError: data.importError || null,
    submittedAtMs: data.submittedAtMs,
  });
}

// 'resumeState': the server's history replaces whatever was shown.
export function restoreAttempts(list) {
  attempts = (list || []).slice();
  const last = attempts[attempts.length - 1];
  selectedAttempt = last ? last.attempt : null;
  renderSubmissions();
  renderCases();
}

// New race, or Play Again.
export function resetAttempts() {
  attempts = [];
  selectedAttempt = null;
  selectTab('problem');
  renderSubmissions();
}

// The language picker changes the breadcrumb, which files are chips, the
// constraint callout and the Rules Ref; the cases are language-agnostic.
// editor.js has its own listener for the editor itself.
languageSelect.addEventListener('change', () => {
  if (state.problem) renderProblem(state.problem);
});
