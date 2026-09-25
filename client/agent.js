/* ═══════════════════════════════════════════════════
   1v1dev — Agent copilot panel
   Everything agent-related on the client lives here: element refs,
   transcript state, prompting and transcript rendering. The two agent
   sections below are moved verbatim from app.js — per the CLAUDE.md
   house rule, don't change agent code as part of unrelated work.
   ═══════════════════════════════════════════════════ */

import { languageSelect } from './dom.js';
import { escapeHtml } from './util.js';
import { editor } from './editor.js';
import { ws } from './ws.js';

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

// Agent conversation transcript. Each entry: { role, text, code, hasCode,
// counted }. 'counted' marks a successful player/agent pair — the same
// ones that made it into Room.agent_sessions and therefore into what the
// model sees; a failed turn stays visible here for the player's own
// record but is not counted (the server never stored it either).
let transcript = [];
let pendingTranscriptIndex = null; // index of the in-flight 'pending' entry

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

// ── Entry points for the rest of the app ────────────
// The rest of the client never touches agent elements or transcript
// state directly — it goes through these. The bodies are moved verbatim
// from app.js's message handler and resume path, since transcript and
// pendingTranscriptIndex can only be assigned from this module.

// Clears the agent panel for a new race, a resume, or Play Again.
export function resetAgentPanel() {
  setAgentStatus('', false);
  agentInstructionInput.value = '';
  agentAskBtn.disabled = false;
  resetAgentTranscript();
}

// 'agentResponse' message.
export function handleAgentResponse(data) {
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
}

// 'agentStatus' message.
export function handleAgentStatus(data) {
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
}

// Rebuild the transcript from resumeState.agentTranscript.
export function restoreTranscript(turns) {
  resetAgentTranscript();
  (turns || []).forEach((turn) => {
    if (turn.role === 'user') {
      transcript.push({ role: 'player', text: turn.content, counted: true });
    } else {
      transcript.push({
        role: 'agent',
        text: turn.content,
        code: turn.code,
        hasCode: !!turn.hasCode,
        counted: true,
      });
    }
  });
  renderTranscript();
}
