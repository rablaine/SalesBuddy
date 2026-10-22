// Sales Buddy - Electron preload.
//
// Chromium's built-in find UI is disabled in Electron, so Ctrl+F does nothing by
// default. This preload injects a small browser-style find bar into every page.
//
// Matches are painted with the CSS Custom Highlight API (Range objects +
// ::highlight() pseudo) rather than Electron's webContents.findInPage. findInPage
// moves NATIVE focus onto the matched text in the page after every keystroke,
// which blurs our find input mid-type and can't be reliably restored from the
// renderer. The Highlight API only paints - it never touches focus or mutates the
// DOM - so the input keeps focus and the user can type the whole term.
//
// Runs in the isolated preload world (contextIsolation on, nodeIntegration off).

const { ipcRenderer } = require('electron');

function startInputFocusDiagnostics() {
  if (window !== window.top) return;
  const targetIds = new Set([
    'editor', 'content', 'task_subject', 'task_description', 'task_category', 'task_due_date',
    'engTaskTitle', 'engTaskDescription', 'modal_task_category',
    'salesbuddy-find-bar', 'sb-shell-menubar',
  ]);
  const tags = new Set([
    'INPUT', 'TEXTAREA', 'SELECT', 'DIV', 'BUTTON', 'BODY', 'A', 'IFRAME', 'HTML',
    'DOCUMENT', 'WINDOW',
  ]);
  const inputTypes = new Set([
    'text', 'search', 'email', 'password', 'url', 'tel', 'number', 'date', 'time',
    'datetime-local', 'checkbox', 'radio', 'file', 'submit', 'button', 'hidden', 'range',
    'color', 'month', 'week', 'reset',
  ]);
  const eventTypes = [
    'focus', 'blur', 'focusin', 'focusout', 'pointerdown', 'pointerup', 'keydown', 'keyup',
    'beforeinput', 'input', 'change',
    'compositionstart', 'compositionend', 'visibilitychange',
  ];
  const aggregateTypes = new Set(['keydown', 'keyup', 'beforeinput', 'input']);
  const selectEvents = new Set(['pointerdown', 'keydown', 'change', 'focusout']);
  let lastSelectFlush = -Infinity;
  const aggregates = new Map();
  let events = [];
  let dropped = 0;
  let maxDelayMs = 0;
  let lastTick = performance.now();
  let lastHeartbeat = lastTick;
  let stopped = false;
  let failed = false;

  function targetInfo(element) {
    const id = targetIds.has(element?.id) ? element.id : '';
    const name = targetIds.has(element?.name) ? element.name : '';
    let area = 'other';
    if (element?.closest) {
      if (element.closest('.ql-editor, #editor')) area = 'note-editor';
      else if (['task_subject', 'task_description', 'task_category', 'task_due_date',
        'modal_task_category'].some(field => field === id || field === name)) {
        area = 'milestone-task';
      }
      else if (element.closest('#salesbuddy-find-bar')) area = 'shell-find';
      else if (element.closest('#sb-shell-menubar')) area = 'shell-menu';
    }
    const tag = element === window ? 'WINDOW' : element === document ? 'DOCUMENT'
      : tags.has(element?.tagName) ? element.tagName : 'OTHER';
    return {
      tag, id, name, area, type: inputTypes.has(element?.type) ? element.type : '',
      editable: element?.isContentEditable === true,
      disabled: element?.disabled === true, readOnly: element?.readOnly === true,
    };
  }
  function add(type, target) {
    const info = targetInfo(target);
    const focused = document.hasFocus();
    const active = targetInfo(document.activeElement);
    const key = aggregateTypes.has(type)
      ? type + JSON.stringify([info, focused, active]) : null;
    const previous = key && aggregates.get(key);
    if (previous) {
      previous.count = Math.min(1000000, previous.count + 1);
      return;
    }
    const entry = {type, at: Date.now(), count: 1, target: info, focused, active};
    if (events.length === 64) {
      const removed = events.shift();
      for (const [storedKey, stored] of aggregates) {
        if (stored === removed) aggregates.delete(storedKey);
      }
      dropped++;
    }
    events.push(entry);
    if (key) aggregates.set(key, entry);
  }
  function flush() {
    if (!events.length && !dropped) return;
    try {
      ipcRenderer.send('shell:input-focus', {
        events, dropped, maxDelayMs, focused: document.hasFocus(),
        visibility: document.visibilityState, active: targetInfo(document.activeElement),
      });
      events = [];
      aggregates.clear();
      dropped = 0;
      maxDelayMs = 0;
      failed = false;
    } catch {
      if (!failed) console.error('Could not send input focus diagnostics to the desktop shell.');
      failed = true;
    }
  }
  function onEvent(event) {
    add(event.type, event.target);
    if (event.target?.tagName === 'SELECT' && selectEvents.has(event.type)
        && performance.now() - lastSelectFlush >= 500) {
      lastSelectFlush = performance.now();
      flush();
    }
  }
  function snapshot() {
    if (stopped) return;
    add('snapshot', document.activeElement);
    flush();
  }
  for (const type of eventTypes) document.addEventListener(type, onEvent, true);
  window.addEventListener('focus', onEvent);
  window.addEventListener('blur', onEvent);
  ipcRenderer.on('shell:input-focus-snapshot', snapshot);
  const timer = setInterval(() => {
    const tick = performance.now();
    maxDelayMs = Math.max(maxDelayMs, tick - lastTick - 1000, 0);
    lastTick = tick;
    if (tick - lastHeartbeat >= 15000) {
      add('heartbeat', document.activeElement);
      lastHeartbeat = tick;
    }
    flush();
  }, 1000);
  function stop() {
    if (stopped) return;
    add('pagehide', document.activeElement);
    flush();
    stopped = true;
    clearInterval(timer);
    for (const type of eventTypes) document.removeEventListener(type, onEvent, true);
    window.removeEventListener('focus', onEvent);
    window.removeEventListener('blur', onEvent);
    ipcRenderer.removeListener('shell:input-focus-snapshot', snapshot);
    window.removeEventListener('pagehide', stop);
  }
  window.addEventListener('pagehide', stop);
  snapshot();
}

startInputFocusDiagnostics();
window.addEventListener('pageshow', event => {
  if (event.persisted) startInputFocusDiagnostics();
});

ipcRenderer.on('shell:loading', (_event, loading) => {
  if (!document.documentElement) return;
  document.documentElement.dataset.sbShellLoading = String(loading === true);
  window.dispatchEvent(new Event('sb:shell-loading'));
});

const FIND_BAR_ID = 'salesbuddy-find-bar';
const HL_ALL = 'sb-find-all';
const HL_ACTIVE = 'sb-find-active';

function positionFindBar(bar) {
  const chromeBottom = Math.max(0, ...['sb-shell-titlebar', 'sb-shell-menubar']
    .map(id => document.getElementById(id)?.getBoundingClientRect().bottom || 0));
  bar.style.top = `${chromeBottom + 12}px`;
}

function createFindBar() {
  if (document.getElementById(FIND_BAR_ID)) return;

  const style = document.createElement('style');
  style.textContent = `
    #${FIND_BAR_ID} {
      align-items: center;
      background: #ffffff;
      border: 1px solid #c7cdd4;
      border-radius: 6px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.22);
      display: none;
      gap: 4px;
      padding: 6px;
      position: fixed;
      right: 12px;
      top: 12px;
      -webkit-app-region: no-drag;
      z-index: 2147483647;
    }
    #${FIND_BAR_ID}.is-visible { display: flex; }
    #${FIND_BAR_ID} input {
      background: #ffffff;
      border: 1px solid #8c959f;
      border-radius: 4px;
      color: #1f2328;
      font: 14px/1.4 "Segoe UI", sans-serif;
      height: 32px;
      outline: none;
      padding: 4px 8px;
      width: 240px;
    }
    #${FIND_BAR_ID} input:focus {
      border-color: #0969da;
      box-shadow: 0 0 0 2px rgba(9, 105, 218, 0.2);
    }
    #${FIND_BAR_ID} .find-count {
      color: #57606a;
      font: 12px/1 "Segoe UI", sans-serif;
      min-width: 58px;
      text-align: center;
    }
    #${FIND_BAR_ID} button {
      align-items: center;
      background: transparent;
      border: 0;
      border-radius: 4px;
      color: #1f2328;
      cursor: pointer;
      display: inline-flex;
      font: 18px/1 "Segoe UI Symbol", sans-serif;
      height: 32px;
      justify-content: center;
      padding: 0;
      width: 32px;
    }
    #${FIND_BAR_ID} button:hover { background: #eaeef2; }
    ::highlight(${HL_ALL}) { background: #ffe08a; color: #1f2328; }
    ::highlight(${HL_ACTIVE}) { background: #ff9632; color: #1f2328; }
    @media (prefers-color-scheme: dark) {
      #${FIND_BAR_ID} { background: #212529; border-color: #495057; }
      #${FIND_BAR_ID} input { background: #111418; border-color: #6c757d; color: #f8f9fa; }
      #${FIND_BAR_ID} .find-count { color: #adb5bd; }
      #${FIND_BAR_ID} button { color: #f8f9fa; }
      #${FIND_BAR_ID} button:hover { background: #343a40; }
    }
  `;
  document.head.appendChild(style);

  const bar = document.createElement('div');
  bar.id = FIND_BAR_ID;
  bar.setAttribute('role', 'search');
  bar.innerHTML = `
    <input type="text" aria-label="Find in page" autocomplete="off" spellcheck="false" />
    <span class="find-count" aria-live="polite"></span>
    <button type="button" data-action="previous" title="Previous match (Shift+Enter)" aria-label="Previous match">&#8593;</button>
    <button type="button" data-action="next" title="Next match (Enter)" aria-label="Next match">&#8595;</button>
    <button type="button" data-action="close" title="Close (Esc)" aria-label="Close">&#215;</button>
  `;
  document.body.appendChild(bar);

  const input = bar.querySelector('input');
  const count = bar.querySelector('.find-count');

  const supportsHighlights = typeof CSS !== 'undefined' && CSS.highlights &&
    typeof Highlight !== 'undefined';

  let matches = [];      // Range per match, in document order
  let activeIndex = -1;

  const updateCount = () => {
    if (matches.length) {
      count.textContent = `${activeIndex + 1}/${matches.length}`;
    } else {
      count.textContent = input.value ? '0/0' : '';
    }
  };

  const clearHighlights = () => {
    matches = [];
    activeIndex = -1;
    if (supportsHighlights) {
      CSS.highlights.delete(HL_ALL);
      CSS.highlights.delete(HL_ACTIVE);
    }
  };

  const isVisible = (el) => {
    if (!el) return false;
    if (el.offsetParent !== null) return true;   // laid out and not display:none
    return el.getClientRects().length > 0;        // covers position:fixed
  };

  const collectTextNodes = () => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return NodeFilter.FILTER_REJECT;
        if (parent.closest(`#${FIND_BAR_ID}`)) return NodeFilter.FILTER_REJECT;
        if (!isVisible(parent)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    return nodes;
  };

  const paintActive = () => {
    if (!supportsHighlights) return;
    if (activeIndex < 0 || !matches[activeIndex]) {
      CSS.highlights.delete(HL_ACTIVE);
      return;
    }
    const range = matches[activeIndex];
    const active = new Highlight(range);
    active.priority = 1;                 // paints over the all-matches layer
    CSS.highlights.set(HL_ACTIVE, active);
    const el = range.startContainer.parentElement;
    if (el) el.scrollIntoView({ block: 'center', inline: 'nearest' });
  };

  const runSearch = (query) => {
    clearHighlights();
    if (!supportsHighlights || !query) { updateCount(); return; }
    const needle = query.toLowerCase();
    for (const node of collectTextNodes()) {
      const haystack = node.nodeValue.toLowerCase();
      let idx = haystack.indexOf(needle);
      while (idx !== -1) {
        const range = document.createRange();
        range.setStart(node, idx);
        range.setEnd(node, idx + needle.length);
        matches.push(range);
        idx = haystack.indexOf(needle, idx + needle.length);
      }
    }
    if (matches.length) {
      CSS.highlights.set(HL_ALL, new Highlight(...matches));
      activeIndex = 0;
      paintActive();
    }
    updateCount();
  };

  const step = (delta) => {
    if (!matches.length) return;
    activeIndex = (activeIndex + delta + matches.length) % matches.length;
    paintActive();
    updateCount();
  };

  // Debounce the incremental (type-ahead) search: each run walks every text node
  // on the page, so on heavy pages coalescing keystrokes avoids piling up walks.
  let searchTimer = null;
  const scheduleSearch = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(input.value), 120);
  };

  const show = () => {
    positionFindBar(bar);
    bar.classList.add('is-visible');
    input.focus();
    input.select();
    if (input.value) runSearch(input.value);
  };
  const close = () => {
    clearTimeout(searchTimer);
    bar.classList.remove('is-visible');
    clearHighlights();
    updateCount();
  };

  input.addEventListener('input', scheduleSearch);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      // Commit any pending debounced search before stepping.
      clearTimeout(searchTimer);
      if (!matches.length && input.value) runSearch(input.value);
      else step(event.shiftKey ? -1 : 1);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });
  bar.addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    if (btn.dataset.action === 'previous') { step(-1); input.focus(); }
    if (btn.dataset.action === 'next') { step(1); input.focus(); }
    if (btn.dataset.action === 'close') close();
  });

  // Capture-phase Ctrl+F so it wins even when a page input has focus.
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      show();
    } else if (event.key === 'Escape' && bar.classList.contains('is-visible')) {
      event.preventDefault();
      close();
    }
  }, true);

  ipcRenderer.on('find:show', show);
  window.addEventListener('resize', () => positionFindBar(bar));
}

window.addEventListener('DOMContentLoaded', createFindBar);

function mountShellTitleBar() {
  if (process.platform !== 'win32' || window !== window.top) return;
  if (document.getElementById('sb-shell-titlebar')) return;
  const root = document.documentElement;
  const style = document.createElement('style');
  style.textContent = `
    #sb-shell-titlebar {
      box-sizing: border-box;
      display: flex;
      align-items: center;
      flex: 0 0 32px;
      height: 32px;
      padding: 0 150px 0 12px;
      background: var(--sb-ui-header, #292d32);
      color: var(--sb-ui-ink, #e7eaf0);
      font: 12px/1 "Segoe UI", sans-serif;
      -webkit-app-region: drag;
      user-select: none;
      overflow: hidden;
    }
    #sb-shell-titlebar span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  `;
  document.head.appendChild(style);
  const bar = document.createElement('header');
  bar.id = 'sb-shell-titlebar';
  bar.setAttribute('aria-label', 'Window title');
  const title = document.createElement('span');
  title.textContent = document.title || 'Sales Buddy';
  bar.appendChild(title);
  document.body.prepend(bar);
  mountShellMenu(bar);
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  function color(token) {
    const value = getComputedStyle(root).getPropertyValue(token).trim();
    if (!value || !CSS.supports('color', value)) throw new Error(`Missing theme token ${token}`);
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const pixel = context.getImageData(0, 0, 1, 1).data;
    return '#' + [...pixel].slice(0, 3).map(n => n.toString(16).padStart(2, '0')).join('');
  }
  function syncTheme() {
    try {
      ipcRenderer.send('shell:theme', {
        mode: root.dataset.sbMode,
        family: root.dataset.sbFamily,
        color: color('--sb-ui-header'),
        symbolColor: color('--sb-ui-ink'),
        backgroundColor: color('--sb-ui-canvas'),
      });
    } catch (error) {
      console.error('Could not synchronize Sales Buddy shell theme:', error);
    }
  }
  let themeReady = document.readyState !== 'loading';
  new MutationObserver(() => {
    if (themeReady) syncTheme();
  }).observe(root, {
    attributes: true, attributeFilter: ['data-sb-mode', 'data-sb-family'],
  });
  new MutationObserver(() => {
    const nextTitle = document.title || 'Sales Buddy';
    if (title.textContent !== nextTitle) title.textContent = nextTitle;
  }).observe(document.head, {childList: true, subtree: true, characterData: true});
  if (themeReady) syncTheme();
  else window.addEventListener('DOMContentLoaded', () => {
    themeReady = true;
    syncTheme();
  }, {once: true});
}

function startShellChrome() {
  if (process.platform !== 'win32' || window !== window.top) return;
  if (document.head && document.body) {
    mountShellTitleBar();
    return;
  }
  const observer = new MutationObserver(() => {
    if (!document.head || !document.body) return;
    observer.disconnect();
    mountShellTitleBar();
  });
  observer.observe(document, {childList: true, subtree: true});
}

startShellChrome();

function mountShellMenu(titleBar) {
  const style = document.createElement('style');
  style.textContent = `
    #sb-shell-menubar {
      box-sizing: border-box;
      display: flex;
      align-items: center;
      flex: 0 0 28px;
      height: 28px;
      padding: 0 4px;
      background: var(--sb-ui-header, #292d32);
      border-bottom: 1px solid var(--sb-ui-line, #414954);
      color: var(--sb-ui-ink, #e7eaf0);
      font: 12px/1 "Segoe UI", sans-serif;
      -webkit-app-region: no-drag;
    }
    #sb-shell-menubar button {
      height: 26px;
      padding: 0 10px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: inherit;
      font: inherit;
    }
    #sb-shell-menubar:not(:has([aria-expanded="true"])) button:hover:not(:disabled) {
      background: var(--sb-ui-raised, #38404a);
    }
    #sb-shell-menubar button:focus-visible {
      outline: 1px solid var(--sb-ui-accent, #9bbfe8);
      outline-offset: -2px;
    }
    #sb-shell-menubar button[aria-expanded="true"] {
      background: var(--sb-ui-raised, #38404a);
    }
    #sb-shell-menubar button:disabled { color: var(--sb-ui-muted, #aab4c0); opacity: .5; }
    #sb-shell-menu-error { padding: 0 12px; color: var(--sb-ui-danger, #f1a3a3); }
  `;
  document.head.appendChild(style);
  const menuBar = document.createElement('nav');
  menuBar.id = 'sb-shell-menubar';
  menuBar.setAttribute('role', 'menubar');
  menuBar.setAttribute('aria-label', 'Desktop application menu');
  titleBar.after(menuBar);
  const error = document.createElement('span');
  error.id = 'sb-shell-menu-error';
  error.setAttribute('role', 'alert');
  let buttons = [];
  let activeMenu = null;
  let contentFocus = document.activeElement;
  document.addEventListener('focusin', event => {
    if (!menuBar.contains(event.target)) contentFocus = event.target;
  });
  function report(message) {
    error.textContent = message;
    console.error('Sales Buddy shell menu:', message);
  }
  async function open(button, toggle = false) {
    if (!button || button.disabled) return;
    error.textContent = '';
    const rect = button.getBoundingClientRect();
    try {
      contentFocus?.focus({preventScroll: true});
      const result = await ipcRenderer.invoke('shell:menu-open', {
        index: Number(button.dataset.index), x: rect.left, y: rect.bottom,
        toggle,
        rects: buttons.filter(button => button.hasAttribute('aria-haspopup')).map(button => {
          const bounds = button.getBoundingClientRect();
          return {index: Number(button.dataset.index), left: bounds.left, right: bounds.right,
            top: bounds.top, bottom: bounds.bottom};
        }),
      });
      if (result.error) report(result.error);
    } catch (failure) {
      report(`Could not open the application menu. ${failure.message}`);
    }
  }
  function render(items) {
    const activeIndex = menuBar.contains(document.activeElement)
      ? document.activeElement.dataset.index : undefined;
    menuBar.replaceChildren();
    buttons = items.filter(item => item.visible).map(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.label.replace(/&/g, '');
      button.dataset.index = String(item.index);
      button.disabled = !item.enabled;
      button.setAttribute('role', 'menuitem');
      if (item.submenu) {
        button.setAttribute('aria-haspopup', 'menu');
        button.setAttribute('aria-expanded', String(item.index === activeMenu));
      }
      button.tabIndex = item.index === 0 ? 0 : -1;
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => open(button, true));
      menuBar.appendChild(button);
      return button;
    });
    menuBar.appendChild(error);
    if (activeIndex !== undefined) {
      buttons.find(button => button.dataset.index === activeIndex)?.focus();
    }
  }
  menuBar.addEventListener('keydown', event => {
    const enabled = buttons.filter(button => !button.disabled);
    if (!enabled.length) return;
    const index = enabled.indexOf(document.activeElement);
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 :
        (index + (event.key === 'ArrowRight' ? 1 : -1) + enabled.length) % enabled.length;
      for (const button of buttons) button.tabIndex = -1;
      enabled[next].tabIndex = 0;
      enabled[next].focus();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open(document.activeElement);
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'F10' && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      buttons.find(button => !button.disabled)?.focus();
    } else if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.length === 1) {
      const button = buttons.find(button =>
        button.getAttribute('aria-haspopup') && button.textContent[0].toLowerCase() ===
          event.key.toLowerCase());
      if (button) {
        event.preventDefault();
        open(button);
      }
    }
  }, true);
  ipcRenderer.on('shell:menu-state', (_event, items) => render(items));
  ipcRenderer.on('shell:menu-active', (_event, index) => {
    activeMenu = index;
    for (const button of buttons) {
      if (button.hasAttribute('aria-haspopup')) {
        button.setAttribute('aria-expanded', String(Number(button.dataset.index) === index));
      }
    }
  });
  ipcRenderer.on('shell:menu-error', (_event, message) => report(message));
  try {
    const result = ipcRenderer.sendSync('shell:menu-initial');
    if (result.error) report(result.error);
    else render(result.items);
  } catch (failure) {
    report(`Could not load the application menu. ${failure.message}`);
  }
}
