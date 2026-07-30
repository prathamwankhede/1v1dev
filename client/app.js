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
const countdownOverlay = document.getElementById('countdown-overlay');
const countdownNumber = document.getElementById('countdown-number');

// Agent panel
const agentTypeSelect = document.getElementById('agent-type-select');
const agentModelInput = document.getElementById('agent-model-input');
const agentBaseUrlInput = document.getElementById('agent-baseurl-input');
const agentApiKeyInput = document.getElementById('agent-apikey-input');
const agentInstructionInput = document.getElementById('agent-instruction-input');
const agentAskBtn = document.getElementById('agent-ask-btn');
const agentStatusEl = document.getElementById('agent-status');

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
    raceTimer.textContent = formatTime(elapsed);
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
    disconnected: 'Disconnected',
    'agent-thinking': 'Agent Thinking',
    'using-agent': 'Using Agent',
  };
  opponentStatusText.textContent = labels[status] || status;
}

// ── Agent Prompting ─────────────────────────────────
// The agent is a copilot the player directs — it never submits on its own.
// A response only loads code into the editor for the player to review.

agentTypeSelect.addEventListener('change', () => {
  const isCustom = agentTypeSelect.value === 'openai-compatible';
  agentBaseUrlInput.style.display = isCustom ? '' : 'none';
});

function setAgentStatus(message, isError) {
  agentStatusEl.textContent = message || '';
  agentStatusEl.classList.toggle('error', !!isError);
}

agentAskBtn.addEventListener('click', () => {
  const instruction = agentInstructionInput.value.trim();
  if (!instruction || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!editor) return;

  agentAskBtn.disabled = true;
  setAgentStatus('Asking agent...', false);

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
    resultSubtitle.textContent = 'You were the fastest coder!';
  } else if (outcome === 'lose') {
    resultIcon.textContent = '💀';
    resultTitle.textContent = 'DEFEAT';
    resultSubtitle.textContent = 'Your opponent beat you this time.';
  } else {
    resultIcon.textContent = '🤝';
    resultTitle.textContent = 'TIE';
    resultSubtitle.textContent = 'Neither player submitted in time.';
  }

  // Player details
  resultYourName.textContent = playerName;
  resultYourTime.textContent = mySub && mySub.submitted
    ? formatTime(mySub.timeMs)
    : 'Did not submit';

  const oppName = oppSub ? oppSub.player : 'Opponent';
  resultOppName.textContent = oppName;
  resultOppTime.textContent = oppSub && oppSub.submitted
    ? formatTime(oppSub.timeMs)
    : 'Did not submit';

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
      // Enable submit button
      submitBtn.disabled = false;
      // Start client-side timer
      startTimer();
      break;

    case 'opponentStatus':
      setOpponentStatus(data.status);
      break;

    case 'submitted':
      // Our own submission was confirmed
      submitBtn.disabled = true;
      submitBtn.innerHTML = `
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M20 6L9 17l-5-5"/>
        </svg>
        Submitted
      `;
      break;

    case 'timeout':
      // Race timed out
      break;

    case 'result':
      showResult(data);
      break;

    case 'agentResponse':
      agentAskBtn.disabled = false;
      setAgentStatus('Agent responded — review before submitting.', false);
      if (editor) editor.setValue(data.code || '');
      agentInstructionInput.value = '';
      break;

    case 'agentStatus':
      agentAskBtn.disabled = false;
      if (data.status === 'error') {
        setAgentStatus(data.message || 'Agent error.', true);
      }
      break;

    case 'error':
      console.warn('Server error:', data.message);
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
submitBtn.addEventListener('click', () => {
  if (hasSubmitted || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!editor) return;

  const code = editor.getValue();
  const language = languageSelect.value;

  ws.send(JSON.stringify({ type: 'submit', code, language }));
  hasSubmitted = true;
  submitBtn.disabled = true;
});

// Play Again button
playAgainBtn.addEventListener('click', () => {
  // Reset state
  hasSubmitted = false;
  problem = null;
  stopTimer();
  raceTimer.textContent = '00:00';
  submitBtn.disabled = true;
  submitBtn.innerHTML = `
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/>
    </svg>
    Submit Solution
  `;
  findMatchBtn.textContent = 'Find Match';
  findMatchBtn.disabled = false;
  lobbyMessage.textContent = 'Ready for another round!';
  lobbyMessage.classList.remove('pulse');
  setAgentStatus('', false);
  agentInstructionInput.value = '';
  agentAskBtn.disabled = false;

  // Tell server we want to play again (removes from old room)
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'playAgain' }));
  }

  showView('lobby');
});

// ── Init ────────────────────────────────────────────
connect();
