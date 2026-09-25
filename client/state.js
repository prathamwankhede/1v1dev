/* ═══════════════════════════════════════════════════
   1v1dev — Shared client state
   ═══════════════════════════════════════════════════ */

// An imported `let` is read-only in the importing module, so anything
// assigned from more than one module lives on this object instead
// (state.problem = ...). State owned by a single module stays private to it.
export const state = {
  currentView: 'lobby',
  playerName: '',
  opponentName: '',
  problem: null,
  hasSubmitted: false,
  timeLimitMs: null,   // from the problem; null → count up as before
  attemptCount: 0,

  // Session identity + working-tree sync (Phase 4). The token is minted by
  // the server on 'join' and is what survives a dropped connection or a
  // refresh — see phase4_session_reconnect_plan.md.
  sessionToken: null,
  workingRev: 0,
  dirtySince: null,
};
