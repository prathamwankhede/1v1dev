/* ═══════════════════════════════════════════════════
   1v1dev — Client Application Logic
   ═══════════════════════════════════════════════════ */

// ── DOM References ──────────────────────────────────
const views = {
  lobby: document.getElementById('view-lobby'),
  race: document.getElementById('view-race'),
  result: document.getElementById('view-result'),
};

// Lobby
const statusPill = document.getElementById('status-pill');
const playerCount = document.getElementById('player-count');
const lobbyMessage = document.getElementById('lobby-message');
const playerNameInput = document.getElementById('player-name-input');
const findMatchBtn = document.getElementById('find-match-btn');

// Race
const raceTimer = document.getElementById('race-timer');
const opponentNameEl = document.getElementById('opponent-name');
const opponentStatusBadge = document.getElementById('opponent-status-badge');
const opponentStatusText = document.getElementById('opponent-status-text');
const problemTitle = document.getElementById('problem-title');
const problemDescription = document.getElementById('problem-description');
const testCasesContainer = document.getElementById('test-cases-container');
const languageSelect = document.getElementById('language-select');
const editorContainer = document.getElementById('editor-container');
const submitBtn = document.getElementById('submit-btn');
const verdictPanel = document.getElementById('verdict-panel');
const countdownOverlay = document.getElementById('countdown-overlay');
const countdownNumber = document.getElementById('countdown-number');

// The submit button's resting markup, restored after a rejected attempt.
const SUBMIT_LABEL = submitBtn ? submitBtn.innerHTML : 'Submit Solution';

// Agent panel
const agentTypeSelect = document.getElementById('agent-type-select');
const agentModelInput = document.getElementById('agent-model-input');
const agentBaseUrlInput = document.getElementById('agent-baseurl-input');
const agentApiKeyInput = document.getElementById('agent-apikey-input');
const agentInstructionInput = document.getElementById('agent-instruction-input');
const agentAskBtn = document.getElementById('agent-ask-btn');
const agentStatusEl = document.getElementById('agent-status');
const agentTranscript = document.getElementById('agent-transcript');
const agentTranscriptToggle = document.getElementById('agent-transcript-toggle');
const agentTranscriptTitle = document.getElementById('agent-transcript-title');
const agentTranscriptBody = document.getElementById('agent-transcript-body');

// Must match Room.AGENT_HISTORY_LIMIT (server/room.py) — the transcript
// keeps every turn from the race, but the model only ever sees the most
// recent AGENT_HISTORY_LIMIT of them.
const AGENT_HISTORY_LIMIT = 20;

// Result
const resultCard = document.querySelector('.result-card');
const resultIcon = document.getElementById('result-icon');
const resultTitle = document.getElementById('result-title');
const resultSubtitle = document.getElementById('result-subtitle');
const resultYourName = document.getElementById('result-your-name');
const resultYourTime = document.getElementById('result-your-time');
const resultOppName = document.getElementById('result-opp-name');
const resultOppTime = document.getElementById('result-opp-time');
const playAgainBtn = document.getElementById('play-again-btn');

// ── State ───────────────────────────────────────────
let ws = null;
let editor = null;
let currentView = 'lobby';
let playerName = '';
let opponentName = '';
let problem = null;
let raceStartTime = null;
let timerInterval = null;
let hasSubmitted = false;
let timeLimitMs = null;   // from the problem; null → count up as before
let attemptCount = 0;

// Agent conversation transcript. Each entry: { role, text, code, hasCode,
// counted }. 'counted' marks a successful player/agent pair — the same
// ones that made it into Room.agent_sessions and therefore into what the
// model sees; a failed turn stays visible here for the player's own
// record but is not counted (the server never stored it either).
let transcript = [];
let pendingTranscriptIndex = null; // index of the in-flight 'pending' entry

// ── View Management ─────────────────────────────────
function showView(name) {
  for (const [key, el] of Object.entries(views)) {
    el.classList.toggle('active', key === name);
  }
  currentView = name;
}

// ── Timer ───────────────────────────────────────────
function startTimer() {
  raceStartTime = Date.now();
  timerInterval = setInterval(() => {
    const elapsed = Date.now() - raceStartTime;
    if (timeLimitMs === null) {
      raceTimer.textContent = formatTime(elapsed);
      return;
    }
    // Count down when the problem declares a limit, so players can pace a
    // long implementation problem.
    const remaining = Math.max(0, timeLimitMs - elapsed);
    raceTimer.textContent = formatTime(remaining);
    raceTimer.classList.toggle('urgent', remaining <= 30000);
  }, 100);
}

function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

function formatTime(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

// ── CodeMirror Setup ────────────────────────────────
function initEditor(starterCode, language) {
  // Destroy previous instance if any
  if (editor) {
    editor.toTextArea();
  }

  // Create a textarea for CodeMirror to enhance
  const textarea = document.createElement('textarea');
  textarea.id = 'code-editor';
  editorContainer.innerHTML = '';
  editorContainer.appendChild(textarea);

  const mode = language === 'javascript' ? 'javascript' : 'python';
  const code = starterCode[language] || starterCode.python || '';

  editor = CodeMirror.fromTextArea(textarea, {
    mode: mode,
    theme: 'material-darker',
    lineNumbers: true,
    tabSize: 4,
    indentUnit: 4,
    indentWithTabs: false,
    lineWrapping: true,
    autofocus: true,
    extraKeys: {
      'Tab': (cm) => cm.replaceSelection('    ', 'end'),
    },
  });

  editor.setValue(code);

  // Refresh after a short delay to ensure proper rendering
  setTimeout(() => editor.refresh(), 50);
}

// ── Problem Rendering ───────────────────────────────
function renderProblem(prob) {
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

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── Opponent Status ─────────────────────────────────
function setOpponentStatus(status) {
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
function setSubmitEnabled(enabled, label) {
  submitBtn.disabled = !enabled;
  if (label) submitBtn.innerHTML = label;
}

function renderVerdict(data) {
  const { accepted, passCount, totalTests, results, attempt } = data;
  verdictPanel.style.display = '';
  verdictPanel.className = `verdict-panel ${accepted ? 'accepted' : 'rejected'}`;

  const heading = accepted
    ? `Accepted — ${passCount}/${totalTests} tests passed`
    : `Rejected — ${passCount}/${totalTests} tests passed`;

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
    `<ul class="verdict-list">${rows}</ul>`;
}

// ── Agent Prompting ─────────────────────────────────
// The agent is a copilot the player directs — it never submits on its own.
// A response only loads code into the editor for the player to review.

agentTypeSelect.addEventListener('change', () => {
  const isCustom = agentTypeSelect.value === 'openai-compatible';
  const isLocalClaudeCode = agentTypeSelect.value === 'claude-code';
  agentBaseUrlInput.style.display = isCustom ? '' : 'none';
  agentApiKeyInput.style.display = isLocalClaudeCode ? 'none' : '';
  agentModelInput.placeholder = isLocalClaudeCode
    ? 'Model (optional — e.g. sonnet, opus)'
    : 'Model (e.g. claude-sonnet-5)';
});

function setAgentStatus(message, isError) {
  agentStatusEl.textContent = message || '';
  agentStatusEl.classList.toggle('error', !!isError);
}

// ── Agent Transcript ─────────────────────────────────
// Every turn from the race stays visible here — display and model context
// diverge by design, since the model only ever sees the last
// AGENT_HISTORY_LIMIT messages. Code never reaches the editor on its own;
// the player applies a specific turn's code explicitly.

function resetAgentTranscript() {
  transcript = [];
  pendingTranscriptIndex = null;
  agentTranscript.style.display = 'none';
  agentTranscript.classList.remove('collapsed');
  agentTranscriptBody.innerHTML = '';
}

function renderTranscript() {
  if (transcript.length === 0) {
    agentTranscript.style.display = 'none';
    return;
  }
  agentTranscript.style.display = '';

  const countedTotal = transcript.filter((t) => t.counted).length;
  agentTranscriptTitle.textContent =
    `${countedTotal} ${countedTotal === 1 ? 'turn' : 'turns'} · last ${AGENT_HISTORY_LIMIT} sent to agent`;

  agentTranscriptBody.innerHTML = transcript.map((t, i) => {
    if (t.role === 'player') {
      return `<div class="transcript-turn turn-player">
        <div class="transcript-label">You</div>
        <div class="transcript-text">${escapeHtml(t.text)}</div>
      </div>`;
    }
    if (t.role === 'pending') {
      return `<div class="transcript-turn turn-pending">
        <div class="transcript-label">Agent</div>
        <div class="transcript-text transcript-thinking">Thinking…</div>
      </div>`;
    }
    if (t.role === 'error') {
      return `<div class="transcript-turn turn-error">
        <div class="transcript-label">Agent</div>
        <div class="transcript-text">${escapeHtml(t.text)}</div>
      </div>`;
    }
    // 'agent' — a completed reply. Apply loads *this* turn's code, so an
    // earlier turn stays re-appliable even after later turns arrive.
    const codePreview = t.hasCode
      ? `<pre class="transcript-code">${escapeHtml(t.code)}</pre>
         <button class="btn-apply" type="button" data-turn="${i}">Apply to editor</button>`
      : '';
    return `<div class="transcript-turn turn-agent">
      <div class="transcript-label">Agent</div>
      <div class="transcript-text">${escapeHtml(t.text)}</div>
      ${codePreview}
    </div>`;
  }).join('');

  agentTranscriptBody.scrollTop = agentTranscriptBody.scrollHeight;
}

agentTranscriptToggle.addEventListener('click', () => {
  agentTranscript.classList.toggle('collapsed');
});

// Delegated: turns re-render on every message, so a listener on each
// button would leak/duplicate.
agentTranscriptBody.addEventListener('click', (e) => {
  const btn = e.target.closest('.btn-apply');
  if (!btn || !editor) return;
  const turn = transcript[Number(btn.dataset.turn)];
  if (turn && turn.role === 'agent' && turn.hasCode) {
    editor.setValue(turn.code);
  }
});

agentAskBtn.addEventListener('click', () => {
  const instruction = agentInstructionInput.value.trim();
  if (!instruction || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!editor) return;

  agentAskBtn.disabled = true;
  setAgentStatus('', false);

  // Show the player's own question immediately, while the agent thinks —
  // don't wait for the round trip to render it.
  transcript.push({ role: 'player', text: instruction });
  pendingTranscriptIndex = transcript.length;
  transcript.push({ role: 'pending' });
  renderTranscript();

  ws.send(JSON.stringify({
    type: 'agentPrompt',
    agentType: agentTypeSelect.value,
    model: agentModelInput.value.trim(),
    baseUrl: agentBaseUrlInput.value.trim(),
    apiKey: agentApiKeyInput.value,
    language: languageSelect.value,
    instruction,
    code: editor.getValue(),
  }));

  agentInstructionInput.value = '';
});

// Cmd/Ctrl+Enter sends, mirroring the Enter-to-submit precedent on the
// player-name input.
agentInstructionInput.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !agentAskBtn.disabled) {
    e.preventDefault();
    agentAskBtn.click();
  }
});

// ── Result Rendering ────────────────────────────────
function showResult(data) {
  stopTimer();
  const { winner, submissions } = data;

  // Determine outcome for this player
  const mySub = submissions.find(s => s.player === playerName);
  const oppSub = submissions.find(s => s.player !== playerName);

  let outcome;
  if (!winner) {
    outcome = 'tie';
  } else if (winner === playerName) {
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

  resultYourName.textContent = playerName;
  resultYourTime.textContent = describe(mySub);

  const oppName = oppSub ? oppSub.player : 'Opponent';
  resultOppName.textContent = oppName;
  resultOppTime.textContent = describe(oppSub);

  showView('result');
}

// ── WebSocket Connection ────────────────────────────
function connect() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    statusPill.textContent = 'Online';
    statusPill.className = 'status-pill online';
    lobbyMessage.textContent = 'Enter your handle and find a match!';
    lobbyMessage.classList.remove('pulse');
    findMatchBtn.disabled = !playerNameInput.value.trim();
  };

  ws.onclose = () => {
    statusPill.textContent = 'Offline';
    statusPill.className = 'status-pill offline';
    playerCount.textContent = '0';
    lobbyMessage.textContent = 'Disconnected. Reconnecting...';
    lobbyMessage.classList.add('pulse');
    findMatchBtn.disabled = true;
    stopTimer();
    setTimeout(connect, 3000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleMessage(data);
    } catch (err) {
      console.error('Error parsing message:', err);
    }
  };

  ws.onerror = () => {
    ws.close();
  };
}

// ── Message Handler ─────────────────────────────────
function handleMessage(data) {
  switch (data.type) {
    case 'playerCount':
      playerCount.textContent = data.count;
      break;

    case 'matched':
      opponentName = data.opponent;
      opponentNameEl.textContent = data.opponent;
      hasSubmitted = false;
      setOpponentStatus('writing');
      setAgentStatus('', false);
      agentInstructionInput.value = '';
      agentAskBtn.disabled = false;
      resetAgentTranscript();
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
      problem = data.problem;
      renderProblem(problem);
      // Initialize editor with starter code
      const lang = languageSelect.value;
      initEditor(problem.starterCode || {}, lang);
      // Reset per-race verdict and agent state
      attemptCount = 0;
      verdictPanel.style.display = 'none';
      verdictPanel.innerHTML = '';
      resetAgentTranscript();
      // Enable submit button
      setSubmitEnabled(true, SUBMIT_LABEL);
      // A problem may set its own clock; otherwise keep counting up
      timeLimitMs = problem.timeLimitSeconds
        ? problem.timeLimitSeconds * 1000
        : null;
      // Start client-side timer
      startTimer();
      break;

    case 'opponentStatus':
      setOpponentStatus(data.status);
      break;

    case 'judging':
      // Our attempt is being run against the test cases.
      attemptCount = data.attempt || attemptCount + 1;
      setSubmitEnabled(false, `Judging attempt ${attemptCount}...`);
      break;

    case 'submissionResult':
      renderVerdict(data);
      if (data.accepted) {
        setSubmitEnabled(false, `
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M20 6L9 17l-5-5"/>
          </svg>
          Accepted
        `);
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
      agentAskBtn.disabled = false;
      // The player applies code explicitly from the transcript — a reply
      // never overwrites the editor on its own, so a hand-edit in progress
      // (or an earlier Apply) is never silently discarded.
      if (pendingTranscriptIndex !== null) {
        transcript[pendingTranscriptIndex] = {
          role: 'agent',
          text: data.log || '',
          code: data.code || '',
          hasCode: !!data.hasCode,
          counted: true,
        };
        transcript[pendingTranscriptIndex - 1].counted = true;
        pendingTranscriptIndex = null;
      }
      renderTranscript();
      break;

    case 'agentStatus':
      agentAskBtn.disabled = false;
      if (data.status === 'error') {
        if (pendingTranscriptIndex !== null) {
          // Tied to a turn the player just asked — show it inline rather
          // than as a status line that disappears on the next message.
          transcript[pendingTranscriptIndex] = {
            role: 'error',
            text: data.message || 'Agent error.',
          };
          pendingTranscriptIndex = null;
          renderTranscript();
        } else {
          // Not tied to any turn (e.g. sent outside a race) — nothing in
          // the transcript to attach it to.
          setAgentStatus(data.message || 'Agent error.', true);
        }
      } else {
        setAgentStatus('', false);
      }
      break;

    case 'error':
      console.warn('Server error:', data.message);
      // Rejected submits (resubmit cooldown, judging still in flight) arrive
      // here — silently dropping them would leave the button stuck.
      if (currentView === 'race') {
        verdictPanel.style.display = '';
        verdictPanel.className = 'verdict-panel rejected';
        verdictPanel.innerHTML =
          `<div class="verdict-heading">${escapeHtml(data.message || 'Error')}</div>`;
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
  playerName = playerNameInput.value.trim();
  if (!playerName || !ws || ws.readyState !== WebSocket.OPEN) return;

  ws.send(JSON.stringify({ type: 'join', playerName }));
  findMatchBtn.disabled = true;
  findMatchBtn.textContent = 'Searching...';
  lobbyMessage.textContent = 'Looking for an opponent...';
  lobbyMessage.classList.add('pulse');
});

// Language select
languageSelect.addEventListener('change', () => {
  if (problem && problem.starterCode && editor) {
    const lang = languageSelect.value;
    const mode = lang === 'javascript' ? 'javascript' : 'python';
    const code = problem.starterCode[lang] || '';
    editor.setOption('mode', mode);
    editor.setValue(code);
  }
});

// Submit button
// Resubmission is allowed: a rejected attempt re-enables the button. The
// server is the authority on whether an attempt is accepted at all (it also
// enforces the resubmit cooldown), so we only guard against double-clicks.
submitBtn.addEventListener('click', () => {
  if (submitBtn.disabled || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!editor) return;

  const code = editor.getValue();
  const language = languageSelect.value;

  ws.send(JSON.stringify({ type: 'submit', code, language }));
  hasSubmitted = true;
  setSubmitEnabled(false, 'Submitting...');
});

// Play Again button
playAgainBtn.addEventListener('click', () => {
  // Reset state
  hasSubmitted = false;
  problem = null;
  stopTimer();
  raceTimer.textContent = '00:00';
  raceTimer.classList.remove('urgent');
  timeLimitMs = null;
  attemptCount = 0;
  verdictPanel.style.display = 'none';
  verdictPanel.innerHTML = '';
  setSubmitEnabled(false, SUBMIT_LABEL);
  findMatchBtn.textContent = 'Find Match';
  findMatchBtn.disabled = false;
  lobbyMessage.textContent = 'Ready for another round!';
  lobbyMessage.classList.remove('pulse');
  setAgentStatus('', false);
  agentInstructionInput.value = '';
  agentAskBtn.disabled = false;
  resetAgentTranscript();

  // Tell server we want to play again (removes from old room)
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'playAgain' }));
  }

  showView('lobby');
});

// ── Init ────────────────────────────────────────────
connect();
