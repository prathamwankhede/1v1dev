/* ═══════════════════════════════════════════════════
   1v1dev — Client entry point
   Message dispatch, lobby flow and startup. Feature logic lives in the
   imported modules; see client/CLAUDE.md for the module map.
   ═══════════════════════════════════════════════════ */

import {
  statusPill, playerCount, lobbyMessage, playerNameInput, findMatchBtn,
  raceTimer, opponentNameEl, languageSelect,
  countdownOverlay, countdownNumber, playAgainBtn,
  showView, SUBMIT_LABEL, ACCEPTED_LABEL,
} from './dom.js';
import { formatTime } from './util.js';
import { state } from './state.js';
import { ws, connect } from './ws.js';
import { initEditor, setEditorChangeHandler } from './editor.js';
import {
  loadStoredSession, saveSessionToken, clearStoredSession,
  saveWorkingTree, scheduleSync,
} from './session.js';
import {
  startTimer, stopTimer, renderProblem, setOpponentStatus,
  setSubmitEnabled, renderVerdict, hideVerdict, renderVerdictError,
  showResult,
} from './race.js';
import { handleResumeState } from './resume.js';
import { resetAgentPanel, handleAgentResponse, handleAgentStatus } from './agent.js';

// ── WebSocket Connection ────────────────────────────
function onOpen() {
  statusPill.textContent = 'Online';
  statusPill.className = 'status-pill online';

  // A stored token means there's a race to rejoin — try that before
  // falling back to the normal "enter a name" lobby flow.
  const stored = loadStoredSession();
  if (stored && stored.token) {
    state.sessionToken = stored.token;
    state.workingRev = stored.rev;
    ws.send(JSON.stringify({ type: 'resume', token: stored.token }));
    return;
  }

  lobbyMessage.textContent = 'Enter your handle and find a match!';
  lobbyMessage.classList.remove('pulse');
  findMatchBtn.disabled = !playerNameInput.value.trim();
}

function onClose() {
  statusPill.textContent = 'Offline';
  statusPill.className = 'status-pill offline';
  playerCount.textContent = '0';
  lobbyMessage.textContent = 'Disconnected. Reconnecting...';
  lobbyMessage.classList.add('pulse');
  findMatchBtn.disabled = true;
  stopTimer();
}

// ── Message Handler ─────────────────────────────────
function handleMessage(data) {
  switch (data.type) {
    case 'playerCount':
      playerCount.textContent = data.count;
      break;

    case 'session':
      state.sessionToken = data.token;
      saveSessionToken(data.token);
      break;

    case 'resumeState':
      handleResumeState(data);
      break;

    case 'resumeFailed':
      clearStoredSession();
      state.sessionToken = null;
      showView('lobby');
      lobbyMessage.textContent = data.reason === 'expired'
        ? 'Your previous race is no longer available.'
        : 'Could not resume your session — find a new match.';
      lobbyMessage.classList.remove('pulse');
      findMatchBtn.disabled = !playerNameInput.value.trim();
      break;

    case 'matched':
      state.opponentName = data.opponent;
      opponentNameEl.textContent = data.opponent;
      state.hasSubmitted = false;
      setOpponentStatus('writing');
      resetAgentPanel();
      // Show race view (countdown overlay will be visible on top)
      showView('race');
      countdownOverlay.classList.add('active');
      break;

    case 'countdown':
      countdownNumber.textContent = data.secondsLeft;
      // Re-trigger animation
      countdownNumber.style.animation = 'none';
      // Force reflow
      void countdownNumber.offsetWidth;
      countdownNumber.style.animation = 'countdownPulse 1s ease-in-out';
      break;

    case 'raceStart':
      // Hide countdown overlay
      countdownOverlay.classList.remove('active');
      // Store problem and render
      state.problem = data.problem;
      renderProblem(state.problem);
      // Initialize editor with starter code
      const lang = languageSelect.value;
      initEditor(state.problem.files || {}, lang);
      // Reset per-race verdict and agent state
      state.attemptCount = 0;
      hideVerdict();
      resetAgentPanel();
      // A fresh race means a fresh working buffer — nothing to carry over
      // from whatever the previous race last synced.
      state.workingRev = 0;
      state.dirtySince = null;
      saveWorkingTree(0, {});
      // Enable submit button
      setSubmitEnabled(true, SUBMIT_LABEL);
      // A problem may set its own clock; otherwise keep counting up
      state.timeLimitMs = state.problem.timeLimitSeconds
        ? state.problem.timeLimitSeconds * 1000
        : null;
      // Start client-side timer
      startTimer();
      break;

    case 'opponentStatus':
      setOpponentStatus(data.status);
      break;

    case 'judging':
      // Our attempt is being run against the test cases.
      state.attemptCount = data.attempt || state.attemptCount + 1;
      setSubmitEnabled(false, `Judging attempt ${state.attemptCount}...`);
      break;

    case 'submissionResult':
      renderVerdict(data);
      if (data.accepted) {
        setSubmitEnabled(false, ACCEPTED_LABEL);
      } else {
        // Rejected — the player fixes it and submits again.
        setSubmitEnabled(true, SUBMIT_LABEL);
      }
      break;

    case 'submitted':
      // Phase 1 fallback (no judge): one shot, no verdict to come.
      setSubmitEnabled(false, `
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M20 6L9 17l-5-5"/>
        </svg>
        Submitted
      `);
      break;

    case 'timeout':
      stopTimer();
      raceTimer.textContent = formatTime(0);
      setSubmitEnabled(false, "Time's up");
      break;

    case 'result':
      showResult(data);
      break;

    case 'agentResponse':
      handleAgentResponse(data);
      break;

    case 'agentStatus':
      handleAgentStatus(data);
      break;

    case 'error':
      console.warn('Server error:', data.message);
      // Rejected submits (resubmit cooldown, judging still in flight) arrive
      // here — silently dropping them would leave the button stuck.
      if (state.currentView === 'race') {
        renderVerdictError(data.message);
        setSubmitEnabled(true, SUBMIT_LABEL);
      }
      break;
  }
}

// ── Event Listeners ─────────────────────────────────

// Name input: enable/disable Find Match button
playerNameInput.addEventListener('input', () => {
  const hasName = playerNameInput.value.trim().length > 0;
  findMatchBtn.disabled = !(hasName && ws && ws.readyState === WebSocket.OPEN);
});

// Enter key in name input
playerNameInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !findMatchBtn.disabled) {
    findMatchBtn.click();
  }
});

// Find Match button
findMatchBtn.addEventListener('click', () => {
  state.playerName = playerNameInput.value.trim();
  if (!state.playerName || !ws || ws.readyState !== WebSocket.OPEN) return;

  ws.send(JSON.stringify({ type: 'join', playerName: state.playerName }));
  findMatchBtn.disabled = true;
  findMatchBtn.textContent = 'Searching...';
  lobbyMessage.textContent = 'Looking for an opponent...';
  lobbyMessage.classList.add('pulse');
});

// Play Again button
playAgainBtn.addEventListener('click', () => {
  // Reset state
  state.hasSubmitted = false;
  state.problem = null;
  stopTimer();
  raceTimer.textContent = '00:00';
  raceTimer.classList.remove('urgent');
  state.timeLimitMs = null;
  state.attemptCount = 0;
  hideVerdict();
  setSubmitEnabled(false, SUBMIT_LABEL);
  findMatchBtn.textContent = 'Find Match';
  findMatchBtn.disabled = false;
  lobbyMessage.textContent = 'Ready for another round!';
  lobbyMessage.classList.remove('pulse');
  resetAgentPanel();

  // Leaving this race for good — nothing left to resume into.
  clearStoredSession();
  state.sessionToken = null;

  // Tell server we want to play again (removes from old room)
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'playAgain' }));
  }

  showView('lobby');
});

// ── Init ────────────────────────────────────────────
setEditorChangeHandler(scheduleSync);
connect({ onOpen, onClose, onMessage: handleMessage });
