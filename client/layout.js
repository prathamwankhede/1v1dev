/* ═══════════════════════════════════════════════════
   1v1dev — Race view layout: collapsible sidebars and the
   Copilot settings popover. No agent logic: the popover only shows and
   hides the container that holds agent.js's inputs.
   ═══════════════════════════════════════════════════ */

import { editorContainer } from './dom.js';
import { editor } from './editor.js';

// {left, right}: true = collapsed. A per-browser preference, separate from
// the session keys in session.js, so clearing a session never resets it.
const LAYOUT_KEY = '1v1dev_layout';
const NARROW_RIGHT_PX = 1200;  // below this, start a race with Copilot collapsed
const NARROW_BOTH_PX = 900;    // below this, start with both collapsed

const sidebars = {
  left: document.getElementById('problem-sidebar'),
  right: document.getElementById('copilot-sidebar'),
};
const settingsBtn = document.getElementById('agent-settings-btn');
const settingsPopover = document.getElementById('agent-settings');

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') || {};
    return { left: !!p.left, right: !!p.right };
  } catch (err) {
    return { left: false, right: false };
  }
}

function savePrefs() {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(prefs)); } catch (err) { /* ignore */ }
}

let prefs = loadPrefs();

// persist=false is for automatic collapses (narrow viewport), which apply
// to this page only and must never overwrite what the player chose.
export function setCollapsed(side, collapsed, persist = true) {
  const el = sidebars[side];
  el.classList.toggle('collapsed', collapsed);
  document.querySelectorAll(`[aria-controls="${el.id}"]`).forEach((btn) => {
    btn.setAttribute('aria-expanded', String(!collapsed));
  });
  if (persist) {
    prefs[side] = collapsed;
    savePrefs();
  }
}

// On entering a race: the saved preference, plus narrow-viewport collapse.
export function applyRaceLayout() {
  const width = window.innerWidth;
  setCollapsed('left', prefs.left || width < NARROW_BOTH_PX, false);
  setCollapsed('right', prefs.right || width < NARROW_RIGHT_PX, false);
}

document.querySelectorAll('[data-collapse]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const side = btn.dataset.collapse;
    setCollapsed(side, !sidebars[side].classList.contains('collapsed'));
  });
});

document.getElementById('copilot-toggle').addEventListener('click', () => {
  setCollapsed('right', !sidebars.right.classList.contains('collapsed'));
});

// CodeMirror measures its own width, so after a sidebar collapses (or the
// window resizes) it must be refreshed, or the cursor lands in the wrong
// column. Observing the editor container covers every cause at once.
let refreshQueued = false;
new ResizeObserver(() => {
  if (!editor || refreshQueued) return;
  refreshQueued = true;
  requestAnimationFrame(() => {
    refreshQueued = false;
    editor.refresh();
  });
}).observe(editorContainer);

// ── Copilot settings popover ────────────────────────
function setSettingsOpen(open) {
  settingsPopover.hidden = !open;
  settingsBtn.setAttribute('aria-expanded', String(open));
}

settingsBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  setSettingsOpen(settingsPopover.hidden);
});

document.addEventListener('click', (e) => {
  if (!settingsPopover.hidden && !settingsPopover.contains(e.target)) {
    setSettingsOpen(false);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !settingsPopover.hidden) {
    setSettingsOpen(false);
    settingsBtn.focus();
  }
});

applyRaceLayout();
