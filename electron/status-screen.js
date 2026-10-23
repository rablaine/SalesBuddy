// Build the same bundled document for startup, recovery and update phases.
const fs = require('fs');
const path = require('path');

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

function statusScreenUrl(title, subtitle, theme = {}) {
  const state = theme.recovering ? 'recovering'
    : theme.failed ? 'failed' : /longer than usual/.test(subtitle || '') ? 'slow'
    : /Reconnecting/.test(title || '') ? 'reconnecting'
    : /Building/.test(title || '') ? 'building'
    : /Starting/.test(title || '') ? 'starting' : 'updating';
  const config = {
    state,
    title: title || 'Updating Sales Buddy',
    subtitle: subtitle || 'Pulling the latest version. The app will restart automatically.',
    mode: theme.mode === 'light' ? 'light' : 'dark',
    family: ['graphite', 'midnight', 'harbor', 'carbon'].includes(theme.family)
      ? theme.family : 'graphite',
  };
  let html = fs.readFileSync(path.join(__dirname, 'status.html'), 'utf8');
  const tokens = fs.readFileSync(path.join(__dirname, 'theme-tokens.css'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, 'status.css'), 'utf8');
  const js = fs.readFileSync(path.join(__dirname, 'status.js'), 'utf8');
  html = html.replace('<link rel="stylesheet" href="theme-tokens.css">', `<style>${tokens}</style>`)
    .replace('<link rel="stylesheet" href="status.css">', `<style>${css}</style>`)
    .replace('<script src="status.js"></script>', `<script>${js}</script>`)
    .replace('data-sb-mode="dark"', `data-sb-mode="${config.mode}"`)
    .replace('data-sb-family="graphite"', `data-sb-family="${config.family}"`)
    .replace('data-state="starting"', `data-state="${config.state}"`)
    .replace('id="failure-actions" hidden',
      `id="failure-actions"${theme.failed && theme.managed ? '' : ' hidden'}`)
    .replace('data-status-action="recover" hidden',
      `data-status-action="recover"${theme.recoveryAvailable ? '' : ' hidden'}`)
    .replace('Starting Sales Buddy</h1>', `${escapeHtml(config.title)}</h1>`)
    .replace('Waiting for the local backend. This window will open automatically.</p>',
      `${escapeHtml(config.subtitle)}</p>`);
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

module.exports = { statusScreenUrl };
