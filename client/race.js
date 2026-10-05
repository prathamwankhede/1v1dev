/* ═══════════════════════════════════════════════════
   1v1dev — Race view: timer, matchup, opponent status, submit, result
   ═══════════════════════════════════════════════════ */

import {
  raceTopbar, timerCapsule, timerProgressBar, raceTimer,
  youNameEl, youAvatar, opponentNameEl, opponentAvatar,
  opponentStatusBadge, opponentStatusText,
  languageSelect, submitBtn, attemptStatus,
  resultCard, resultIcon, resultTitle, resultSubtitle,
  resultYourName, resultYourTime, resultOppName, resultOppTime,
  showView,
} from './dom.js';
import { formatTime } from './util.js';
import { state } from './state.js';
import { ws } from './ws.js';
import { editor, bundles, writableFiles } from './editor.js';

let raceStartTime = null;
let timerInterval = null;

// ── Timer ───────────────────────────────────────────
// Shows the clock in the top bar capsule. With a time limit it counts
// down (with REM and a draining progress bar); without one it counts up
// and both are hidden.
export function startTimer(explicitStartTime) {
  raceStartTime = explicitStartTime || Date.now();
  const countUp = state.timeLimitMs === null;
  timerCapsule.classList.toggle('count-up', countUp);
  raceTopbar.classList.toggle('count-up', countUp);
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
    timerProgressBar.style.width = `${(remaining / state.timeLimitMs) * 100}%`;
    setTimerUrgent(remaining <= 30000);
  }, 100);
}

export function setTimerUrgent(urgent) {
  raceTimer.classList.toggle('urgent', urgent);
  timerCapsule.classList.toggle('urgent', urgent);
  raceTopbar.classList.toggle('urgent', urgent);
}

// LIVE marker in the timer capsule; hidden while the socket is down.
export function setTimerLive(live) {
  timerCapsule.classList.toggle('offline', !live);
}

// ── Matchup chips ───────────────────────────────────
function initial(name) {
  const ch = (name || '').trim().charAt(0);
  return ch ? ch.toUpperCase() : '?';
}

export function renderMatchup() {
  youNameEl.textContent = state.playerName || 'you';
  youAvatar.textContent = initial(state.playerName || 'you');
  opponentNameEl.textContent = state.opponentName || 'Opponent';
  opponentAvatar.textContent = initial(state.opponentName || 'Opponent');
}

export function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
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

// Footer status line: the latest attempt's outcome, or a server error
// (resubmit cooldown, judging still in flight). The per-test detail lives
// in the problem sidebar's Submissions tab.
export function setAttemptStatus(text, tone) {
  attemptStatus.textContent = text || '';
  attemptStatus.className = `attempt-status${tone ? ` ${tone}` : ''}`;
}

export function attemptSummaryText(a) {
  if (!a) return '';
  if (a.judged === false) return `Judging attempt ${a.attempt}…`;
  const outcome = a.accepted ? 'accepted' : 'rejected';
  const crash = a.importError ? ' · import error' : '';
  return `Attempt ${a.attempt} ${outcome} · ${a.passCount}/${a.totalTests} passed${crash}`;
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
