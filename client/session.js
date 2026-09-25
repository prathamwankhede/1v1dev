/* ═══════════════════════════════════════════════════
   1v1dev — Session persistence and working-tree sync (Phase 4)
   ═══════════════════════════════════════════════════ */

import { ws } from './ws.js';
import { editor, bundles, writableFiles } from './editor.js';
import { languageSelect } from './dom.js';
import { state } from './state.js';

const SYNC_MIN_INTERVAL_MS = 2000;
const SYNC_MAX_DIRTY_MS = 15000;
const STORAGE_TOKEN_KEY = '1v1dev_token';
const STORAGE_REV_KEY = '1v1dev_rev';
const STORAGE_FILES_KEY = '1v1dev_files';

let lastSyncAt = 0;
let syncDebounceTimer = null;

export function loadStoredSession() {
  try {
    const token = localStorage.getItem(STORAGE_TOKEN_KEY);
    if (!token) return null;
    const rev = parseInt(localStorage.getItem(STORAGE_REV_KEY) || '0', 10);
    const filesRaw = localStorage.getItem(STORAGE_FILES_KEY);
    const files = filesRaw ? JSON.parse(filesRaw) : {};
    return { token, rev: Number.isFinite(rev) ? rev : 0, files };
  } catch (err) {
    return null;
  }
}

export function saveSessionToken(token) {
  try { localStorage.setItem(STORAGE_TOKEN_KEY, token); } catch (err) { /* ignore */ }
}

export function clearStoredSession() {
  try {
    localStorage.removeItem(STORAGE_TOKEN_KEY);
    localStorage.removeItem(STORAGE_REV_KEY);
    localStorage.removeItem(STORAGE_FILES_KEY);
  } catch (err) { /* ignore */ }
}

export function saveWorkingTree(rev, files) {
  try {
    localStorage.setItem(STORAGE_REV_KEY, String(rev));
    localStorage.setItem(STORAGE_FILES_KEY, JSON.stringify(files));
  } catch (err) { /* ignore */ }
}

// Sends every writable file's content for the *active* language only —
// the same map shape the submit payload carries. A language switch with
// unsynced edits in the other language relies on its own next debounced
// sync (or beforeunload) once the player comes back to it.
function doSyncTree() {
  if (!editor || !ws || ws.readyState !== WebSocket.OPEN) return;
  const lang = languageSelect.value;
  if (!bundles[lang]) return;

  state.workingRev += 1;
  const files = writableFiles(lang);
  lastSyncAt = Date.now();
  state.dirtySince = null;
  saveWorkingTree(state.workingRev, files);
  ws.send(JSON.stringify({ type: 'syncTree', rev: state.workingRev, files }));
}

export function scheduleSync() {
  const now = Date.now();
  if (state.dirtySince === null) state.dirtySince = now;
  if (syncDebounceTimer) clearTimeout(syncDebounceTimer);

  if (now - state.dirtySince >= SYNC_MAX_DIRTY_MS) {
    doSyncTree();
    return;
  }
  const wait = Math.max(0, SYNC_MIN_INTERVAL_MS - (now - lastSyncAt));
  syncDebounceTimer = setTimeout(doSyncTree, wait);
}

window.addEventListener('beforeunload', () => {
  // A ws.send() here is best-effort and the browser may tear the socket
  // down before it lands — localStorage is synchronous and reliable, so
  // that's the copy resume() trusts on the next load.
  if (!editor) return;
  try {
    const lang = languageSelect.value;
    if (!bundles[lang]) return;
    const files = writableFiles(lang);
    saveWorkingTree(state.workingRev + (state.dirtySince !== null ? 1 : 0), files);
  } catch (err) { /* ignore */ }
});
