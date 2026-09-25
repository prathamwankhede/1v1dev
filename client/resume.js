/* ═══════════════════════════════════════════════════
   1v1dev — Resume (Phase 4)
   ═══════════════════════════════════════════════════ */

import {
  showView, countdownOverlay, opponentNameEl, languageSelect,
  SUBMIT_LABEL, ACCEPTED_LABEL,
} from './dom.js';
import { state } from './state.js';
import {
  startTimer, renderProblem, renderVerdict, hideVerdict,
  setSubmitEnabled, setOpponentStatus,
} from './race.js';
import { initEditor, bundles, docs } from './editor.js';
import { loadStoredSession } from './session.js';
import { resetAgentPanel, restoreTranscript } from './agent.js';

// Response to a stored token sent as `{type: 'resume'}` on connect. A
// 'result'-typed reply (room already FINISHED) is handled by the existing
// 'result' case in app.js — this only covers the still-live phases.
export function handleResumeState(data) {
  if (data.phase === 'countdown') {
    // The next countdown tick (or raceStart) will reach this socket now
    // that it's reattached — just get the race view up.
    showView('race');
    countdownOverlay.classList.add('active');
    return;
  }
  if (data.phase !== 'racing') {
    return; // resolving — the result broadcast is moments away
  }

  state.opponentName = data.opponentName || '';
  opponentNameEl.textContent = state.opponentName;
  setOpponentStatus(data.opponentConnected ? 'writing' : 'disconnected');
  resetAgentPanel();

  state.problem = data.problem;
  renderProblem(state.problem);
  const lang = languageSelect.value;
  initEditor(state.problem.files || {}, lang);

  // Restore the player's own working buffer (writable files for the
  // language that was active when it was last synced — same scope
  // doSyncTree writes). localStorage may be ahead of what last reached
  // the server, so whichever rev is higher wins.
  const stored = loadStoredSession();
  const serverRev = data.rev || 0;
  let rev = serverRev;
  let tree = data.tree || {};
  if (stored && stored.rev > serverRev) {
    rev = stored.rev;
    tree = stored.files || {};
  }
  state.workingRev = rev;
  state.dirtySince = null;

  const bundle = bundles[lang];
  if (bundle) {
    bundle.files.forEach((f) => {
      if (!f.writable) return;
      // A tree entry naming a path this problem no longer declares is
      // ignored — nothing to rehydrate it into, and it doesn't get a tab.
      if (Object.prototype.hasOwnProperty.call(tree, f.path)) {
        docs[lang][f.path].setValue(tree[f.path]);
      }
    });
  }

  const attempts = data.attempts || [];
  state.attemptCount = attempts.length;
  const last = attempts[attempts.length - 1];
  if (last && last.judged) {
    renderVerdict({
      accepted: last.accepted,
      passCount: last.passCount,
      totalTests: last.totalTests,
      results: last.results,
      attempt: last.attempt,
      importError: last.importError,
    });
    setSubmitEnabled(!last.accepted, last.accepted ? ACCEPTED_LABEL : SUBMIT_LABEL);
  } else {
    hideVerdict();
    setSubmitEnabled(true, SUBMIT_LABEL);
  }

  restoreTranscript(data.agentTranscript);

  state.timeLimitMs = state.problem.timeLimitSeconds ? state.problem.timeLimitSeconds * 1000 : null;
  const remainingMs = (data.remainingSeconds || 0) * 1000;
  const explicitStart = state.timeLimitMs !== null ? Date.now() - (state.timeLimitMs - remainingMs) : null;
  startTimer(explicitStart);

  countdownOverlay.classList.remove('active');
  showView('race');
}
