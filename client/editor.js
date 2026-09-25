/* ═══════════════════════════════════════════════════
   1v1dev — CodeMirror editor and multi-file buffers
   ═══════════════════════════════════════════════════ */

import { editorContainer, fileTabsEl, languageSelect } from './dom.js';
import { escapeHtml } from './util.js';
import { state } from './state.js';

export let editor = null;

// Multi-file problems (Phase 5). `problem.files[lang]` is always the
// normalized bundle shape now — {entrypoint, files: [{path, content,
// writable}]} — even for a single-file legacy problem (one writable
// file). Both languages' docs stay alive across a language switch, so
// bundles/docs/activePath are keyed by language, not just the current one.
export let bundles = {};      // bundles[lang] = { entrypoint, files: [...] }
export let docs = {};         // docs[lang] = { [path]: CodeMirror.Doc }
export let activePath = {};   // activePath[lang] = "cart.py"

// Set by app.js (to session.js's scheduleSync) so this module doesn't
// import session.js, which already imports this one.
let onChange = null;

export function setEditorChangeHandler(fn) {
  onChange = fn;
}

// ── CodeMirror Setup ────────────────────────────────
// One CodeMirror instance for the whole app lifetime; per-file content
// lives in its own CodeMirror.Doc, and switching files/languages just
// swaps which Doc the editor is currently displaying. This is what keeps
// undo history and unsaved edits alive across a tab or language switch —
// setValue() would discard both.
function ensureEditor() {
  if (editor) return;
  editor = CodeMirror(editorContainer, {
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
  editor.on('change', () => {
    if (onChange) onChange();
  });
}

// Rebuild bundles/docs/activePath from a fresh problem.files payload — a
// new race, or a resume, always starts from the server's starter content.
function buildBundles(problemFiles) {
  bundles = {};
  docs = {};
  activePath = {};
  Object.keys(problemFiles || {}).forEach((lang) => {
    const bundle = problemFiles[lang];
    const mode = lang === 'javascript' ? 'javascript' : 'python';
    bundles[lang] = bundle;
    docs[lang] = {};
    (bundle.files || []).forEach((f) => {
      docs[lang][f.path] = new CodeMirror.Doc(f.content, mode);
    });
    const firstWritable = bundle.files.find((f) => f.writable);
    activePath[lang] = firstWritable ? firstWritable.path : (bundle.files[0] && bundle.files[0].path);
  });
}

function renderFileTabs(lang) {
  const bundle = bundles[lang];
  if (!bundle || bundle.files.length <= 1) {
    fileTabsEl.style.display = 'none';
    fileTabsEl.innerHTML = '';
    return;
  }
  fileTabsEl.style.display = '';
  fileTabsEl.innerHTML = bundle.files.map((f) => {
    const isActive = f.path === activePath[lang];
    const lock = f.writable ? '' : '<span class="tab-lock">\u{1F512}</span>';
    return `<button class="file-tab${isActive ? ' active' : ''}" type="button" data-path="${escapeHtml(f.path)}">${lock}${escapeHtml(f.path)}</button>`;
  }).join('');
}

// Delegated: tabs re-render on every switch, so a listener per button
// would leak/duplicate — same pattern as the transcript's Apply buttons.
fileTabsEl.addEventListener('click', (e) => {
  const btn = e.target.closest('.file-tab');
  if (!btn) return;
  switchToFile(languageSelect.value, btn.dataset.path);
});

function switchToFile(lang, path) {
  const bundle = bundles[lang];
  if (!bundle || !docs[lang] || !docs[lang][path]) return;
  activePath[lang] = path;
  editor.swapDoc(docs[lang][path]);
  const fileMeta = bundle.files.find((f) => f.path === path);
  editor.setOption('readOnly', fileMeta ? !fileMeta.writable : false);
  renderFileTabs(lang);
}

// problemFiles is problem.files — {lang: {entrypoint, files: [...]}}, the
// normalized bundle shape for every language (see server _race_field_whitelist).
export function initEditor(problemFiles, language) {
  ensureEditor();
  buildBundles(problemFiles);

  const path = activePath[language];
  if (path) {
    switchToFile(language, path);
  } else {
    renderFileTabs(language);
  }

  // Refresh after a short delay to ensure proper rendering
  setTimeout(() => editor.refresh(), 50);
}

// Every writable file's content for one language, read from each file's
// own Doc — not editor.getValue(), which only reflects whichever file is
// currently swapped in. The same {path: content} map is what submit and
// syncTree send; the server never sees an entrypoint, it owns that.
// Callers check bundles[lang] exists first.
export function writableFiles(lang) {
  const files = {};
  bundles[lang].files.forEach((f) => {
    if (f.writable) files[f.path] = docs[lang][f.path].getValue();
  });
  return files;
}

// Language select — swaps to the other language's whole bundle. Both
// languages' docs stay alive, so this is non-destructive; no confirm
// dialog needed, unlike the old single-string overwrite.
languageSelect.addEventListener('change', () => {
  if (!state.problem || !editor) return;
  const lang = languageSelect.value;
  const path = activePath[lang];
  if (path) {
    switchToFile(lang, path);
  } else {
    renderFileTabs(lang);
  }
});
