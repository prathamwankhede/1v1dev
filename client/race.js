/* ═══════════════════════════════════════════════════
   1v1dev — Race view: timer, problem, verdicts, submit, result
   ═══════════════════════════════════════════════════ */

import {
  raceTimer, opponentStatusBadge, opponentStatusText,
  problemTitle, problemDescription, testCasesContainer,
  languageSelect, submitBtn, verdictPanel,
  resultCard, resultIcon, resultTitle, resultSubtitle,
  resultYourName, resultYourTime, resultOppName, resultOppTime,
  showView,
} from './dom.js';
import { escapeHtml, formatTime } from './util.js';
import { state } from './state.js';
import { ws } from './ws.js';
import { editor, bundles, writableFiles } from './editor.js';

let raceStartTime = null;
let timerInterval = null;

// ── Timer ───────────────────────────────────────────
export function startTimer(explicitStartTime) {
  raceStartTime = explicitStartTime || Date.now();
  timerInterval = setInterval(() => {
    const elapsed = Date.now() - raceStartTime;
    if (state.timeLimitMs === null) {
      raceTimer.textContent = formatTime(elapsed);
      return;
    }
    // Count down when the problem declares a limit, so players can pace a
    // long implementation problem.
    const remaining = Math.max(0, state.timeLimitMs - elapsed);
    raceTimer.textContent = formatTime(remaining);
    raceTimer.classList.toggle('urgent', remaining <= 30000);
  }, 100);
}

export function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

// ── Problem Rendering ───────────────────────────────
export function renderProblem(prob) {
  problemTitle.textContent = prob.title;
  problemDescription.textContent = prob.description;

  testCasesContainer.innerHTML = '';
  (prob.testCases || []).forEach((tc, i) => {
    const div = document.createElement('div');
    div.className = 'test-case';
    div.innerHTML = `
      <div class="test-case-label">Input</div>
      <pre>${escapeHtml(tc.input)}</pre>
      <div class="test-case-label" style="margin-top: 0.5rem;">Expected Output</div>
      <pre>${escapeHtml(tc.expectedOutput)}</pre>
    `;
    testCasesContainer.appendChild(div);
  });
}

// ── Opponent Status ─────────────────────────────────
export function setOpponentStatus(status) {
  opponentStatusBadge.className = `opp-status ${status}`;
  const labels = {
    writing: 'Writing',
    submitted: 'Submitted',
    attempted: 'Attempt Failed',
    disconnected: 'Disconnected',
    'agent-thinking': 'Agent Thinking',
    'using-agent': 'Using Agent',
  };
  opponentStatusText.textContent = labels[status] || status;
}

// ── Submission Verdicts ─────────────────────────────
// A submission has to pass every test case to be accepted. Anything less
// comes back rejected and the player can fix it and submit again.
export function setSubmitEnabled(enabled, label) {
  submitBtn.disabled = !enabled;
  if (label) submitBtn.innerHTML = label;
}

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

// A server 'error' (resubmit cooldown, judging still in flight) shown in
// the verdict slot, so a rejected submit never leaves the player guessing.
export function renderVerdictError(message) {
  verdictPanel.style.display = '';
  verdictPanel.className = 'verdict-panel rejected';
  verdictPanel.innerHTML =
    `<div class="verdict-heading">${escapeHtml(message || 'Error')}</div>`;
}

// Submit button
// Resubmission is allowed: a rejected attempt re-enables the button. The
// server is the authority on whether an attempt is accepted at all (it also
// enforces the resubmit cooldown), so we only guard against double-clicks.
submitBtn.addEventListener('click', () => {
  if (submitBtn.disabled || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!editor) return;

  const language = languageSelect.value;
  if (!bundles[language]) return;

  const files = writableFiles(language);
  ws.send(JSON.stringify({ type: 'submit', language, files }));
  state.hasSubmitted = true;
  setSubmitEnabled(false, 'Submitting...');
});

// ── Result Rendering ────────────────────────────────
export function showResult(data) {
  stopTimer();
  const { winner, submissions } = data;

  // Determine outcome for this player
  const mySub = submissions.find(s => s.player === state.playerName);
  const oppSub = submissions.find(s => s.player !== state.playerName);

  let outcome;
  if (!winner) {
    outcome = 'tie';
  } else if (winner === state.playerName) {
    outcome = 'win';
  } else {
    outcome = 'lose';
  }

  // Set result card class
  resultCard.className = `result-card ${outcome}`;
  resultTitle.className = `result-title ${outcome}`;

  if (outcome === 'win') {
    resultIcon.textContent = '🏆';
    resultTitle.textContent = 'VICTORY';
    resultSubtitle.textContent = mySub && mySub.passed
      ? 'Your solution passed every test first.'
      : 'You were ahead when time ran out.';
  } else if (outcome === 'lose') {
    resultIcon.textContent = '💀';
    resultTitle.textContent = 'DEFEAT';
    resultSubtitle.textContent = 'Your opponent beat you this time.';
  } else {
    resultIcon.textContent = '🤝';
    resultTitle.textContent = 'TIE';
    resultSubtitle.textContent = 'Neither player got a solution accepted.';
  }

  // Player details. `passed` is null on the no-judge fallback, where there
  // is no test count to report.
  const describe = (sub) => {
    if (!sub || !sub.submitted) return 'Did not submit';
    const time = formatTime(sub.timeMs);
    if (sub.passed === null || sub.passed === undefined) return time;
    const tests = `${sub.passCount}/${sub.totalTests} tests`;
    const tries = sub.attempts > 1 ? `, ${sub.attempts} attempts` : '';
    return `${time} — ${tests}${tries}`;
  };

  resultYourName.textContent = state.playerName;
  resultYourTime.textContent = describe(mySub);

  const oppName = oppSub ? oppSub.player : 'Opponent';
  resultOppName.textContent = oppName;
  resultOppTime.textContent = describe(oppSub);

  showView('result');
}
