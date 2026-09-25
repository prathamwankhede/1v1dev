/* ═══════════════════════════════════════════════════
   1v1dev — DOM references and view switching
   Agent panel elements live in agent.js, not here.
   ═══════════════════════════════════════════════════ */

import { state } from './state.js';

export const views = {
  lobby: document.getElementById('view-lobby'),
  race: document.getElementById('view-race'),
  result: document.getElementById('view-result'),
};

// Lobby
export const statusPill = document.getElementById('status-pill');
export const playerCount = document.getElementById('player-count');
export const lobbyMessage = document.getElementById('lobby-message');
export const playerNameInput = document.getElementById('player-name-input');
export const findMatchBtn = document.getElementById('find-match-btn');

// Race
export const raceTimer = document.getElementById('race-timer');
export const opponentNameEl = document.getElementById('opponent-name');
export const opponentStatusBadge = document.getElementById('opponent-status-badge');
export const opponentStatusText = document.getElementById('opponent-status-text');
export const problemTitle = document.getElementById('problem-title');
export const problemDescription = document.getElementById('problem-description');
export const testCasesContainer = document.getElementById('test-cases-container');
export const languageSelect = document.getElementById('language-select');
export const editorContainer = document.getElementById('editor-container');
export const fileTabsEl = document.getElementById('file-tabs');
export const submitBtn = document.getElementById('submit-btn');
export const verdictPanel = document.getElementById('verdict-panel');
export const countdownOverlay = document.getElementById('countdown-overlay');
export const countdownNumber = document.getElementById('countdown-number');

// The submit button's resting markup, restored after a rejected attempt.
export const SUBMIT_LABEL = submitBtn ? submitBtn.innerHTML : 'Submit Solution';
export const ACCEPTED_LABEL = `
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M20 6L9 17l-5-5"/>
  </svg>
  Accepted
`;

// Result
export const resultCard = document.querySelector('.result-card');
export const resultIcon = document.getElementById('result-icon');
export const resultTitle = document.getElementById('result-title');
export const resultSubtitle = document.getElementById('result-subtitle');
export const resultYourName = document.getElementById('result-your-name');
export const resultYourTime = document.getElementById('result-your-time');
export const resultOppName = document.getElementById('result-opp-name');
export const resultOppTime = document.getElementById('result-opp-time');
export const playAgainBtn = document.getElementById('play-again-btn');

// ── View Management ─────────────────────────────────
export function showView(name) {
  for (const [key, el] of Object.entries(views)) {
    el.classList.toggle('active', key === name);
  }
  state.currentView = name;
}
