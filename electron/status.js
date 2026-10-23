// File previews and the packaged renderer use the same state presentation.
(() => {
  'use strict';
  const root = document.documentElement;
  const params = new URLSearchParams(location.search);
  const states = {
    failed: ['Sales Buddy needs attention', 'The workspace is not ready.', 'Needs attention',
      'Startup stopped. A changed update will recover automatically when its snapshot is verified.'],
    recovering: ['Restoring your previous version',
      'The update failed. We are restoring your saved application and database. Please wait; no action is needed.',
      'Recovering', 'This window will close while the saved version is restored'],
    starting: ['Starting Sales Buddy', 'Waiting for the local backend. This window will open automatically.', 'Starting', 'Checking the local backend automatically'],
    slow: ['Starting Sales Buddy', 'Startup is taking longer than usual. Retrying automatically. Help > Open Logs Folder has details.', 'Still starting', 'Readiness checks continue; no restart needed'],
    reconnecting: ['Reconnecting to Sales Buddy', 'The local backend is unavailable. Retrying automatically. Help > Open Logs Folder has details.', 'Reconnecting', 'Your original page will reopen when the backend is ready'],
    updating: ['Updating Sales Buddy', 'Pulling the latest version. The app will restart automatically.', 'Updating', 'Update in progress; please keep the app open'],
    building: ['Building the desktop app', 'Applying a desktop-app update. This takes a minute or two, then Sales Buddy restarts itself.', 'Building', 'Building the desktop shell before restarting'],
  };
  const requested = params.get('state');
  if (states[requested]) {
    document.body.dataset.state = requested;
    document.getElementById('status-title').textContent = states[requested][0];
    document.getElementById('status-description').textContent = states[requested][1];
  }
  const state = document.body.dataset.state;
  const content = states[state] || states.starting;
  const mode = params.get('mode');
  const family = params.get('family');
  if (['dark', 'light'].includes(mode)) root.dataset.sbMode = mode;
  if (['graphite', 'midnight', 'harbor', 'carbon'].includes(family)) root.dataset.sbFamily = family;
  document.getElementById('status-label').textContent = `Local app / ${content[2]}`;
  document.getElementById('status-detail').textContent = content[3];
  document.querySelector('[role="progressbar"]').setAttribute('aria-label', content[2]);
  if (state === 'failed') {
    document.querySelector('.shell-status').setAttribute('aria-busy', 'false');
    document.querySelector('[role="progressbar"]').hidden = true;
    document.getElementById('status-footer').textContent = 'Readiness has not been confirmed';
    document.querySelector('.shell-help p').textContent =
      'If automatic recovery cannot finish, keep this installation closed and contact the maintainer. Saved diagnostics can help identify the problem.';
  }
  const updating = ['updating', 'building'].includes(state);
  if (updating) {
    document.getElementById('status-footer').textContent = 'Sales Buddy restarts automatically when this finishes';
    document.querySelector('.shell-help p').textContent = 'Keep Sales Buddy open until it restarts. The new web app and background worker must pass readiness checks. Failed updates restore the saved version and queue sanitized diagnostics automatically.';
  }
  if (state === 'recovering') {
    document.getElementById('status-footer').textContent = 'Recovery is automatic; no action is needed';
    document.querySelector('.shell-help p').textContent =
      'Sales Buddy is returning to the version you were using before the update. Diagnostics are saved automatically. You can continue working once restoration finishes.';
  }
})();
