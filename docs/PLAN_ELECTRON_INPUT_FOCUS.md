# Plan: Diagnose Intermittent Electron Input Focus Loss

**Status:** Investigation planned
**Last updated:** 2026-09-22

## Goal

Identify and fix an intermittent production Electron issue where Sales Buddy
text fields temporarily stop accepting input. The fix must address the desktop
shell or Windows focus integration rather than adding page-specific workarounds.

## Reported behavior

- The issue occurs roughly once a week and has appeared on multiple pages,
  including New Note and expanded Activity Coverage rows.
- Clicking a text field can paint a caret at the clicked position, but the field
  does not show its normal focus styling.
- The caret does not blink and typing has no effect.
- Clicks may register only occasionally during an episode.
- The issue usually clears after about 20 seconds.
- Pressing `Win+Ctrl+Shift+B` to reset the graphics driver did not restore input.
- Switching away from Sales Buddy with Alt+Tab and switching back restored input
  immediately during a confirmed episode.
- The confirmed episode occurred in the production Electron application, not the
  browser-based development server.

## Findings so far

### Page code is unlikely

- Affected fields are enabled and writable during normal operation.
- Activity Coverage autosave does not disable its editors.
- Healthy page loads do not contain long browser tasks capable of explaining a
  20-second loss of input.
- No full-screen overlay was active during healthy inspection.
- The same symptom occurs on unrelated pages, which points away from their
  individual JavaScript.

### A GPU stall is now a secondary hypothesis

Windows Error Reporting contains recurring `LiveKernelEvent 193` watchdog dumps,
but no display, NVIDIA, WHEA, or application-hang event was recorded during the
confirmed September 22 episode. Resetting the graphics driver did not help.
These watchdog reports should be investigated separately unless a future input
episode coincides with a new dump.

### Native focus is the leading hypothesis

Alt+Tab fully deactivates and reactivates the Electron window. Its immediate
recovery effect suggests a desynchronized focus chain between Windows, Electron,
Chromium, and the Windows text-input or IME subsystem.

Immediately after recovery, the production application showed:

- A visible, enabled, foreground BrowserWindow.
- A responsive Electron main process, renderer process, and GPU process.
- No native owner window or visible modal attached to the BrowserWindow.
- No update, restart, or dialog activity in `electron-main.log`.
- Normal hidden `Default IME` and `MSCTFIME UI` windows associated with the
  Electron process.

The current production shell does not record enough focus or renderer state to
distinguish among the remaining causes.

## Investigation plan

### Phase 1: Add production diagnostics

Instrument the Electron shell without changing normal user behavior.

In `electron/main.js`, log:

- `BrowserWindow` focus, blur, show, hide, minimize, restore, and closed events.
- App-level `browser-window-focus`, `browser-window-blur`, `activate`, and
  `second-instance` events.
- WebContents `unresponsive`, `responsive`, `render-process-gone`, `did-focus`,
  and `did-blur` events where supported.
- Child process type, reason, and exit code for renderer or GPU termination.
- The foreground window handle and whether the Sales Buddy window is enabled
  when focus changes.

In `electron/preload.js`, maintain a bounded diagnostic record containing:

- `document.hasFocus()` and `document.visibilityState`.
- The active element tag, ID, name, and input type.
- `focus`, `blur`, `focusin`, `focusout`, `pointerdown`, and `keydown` events.
- A lightweight heartbeat that records the largest event-loop delay.

Send diagnostic events to the main process through a narrowly scoped IPC
channel. Do not expose general IPC access to page JavaScript. Write diagnostics
to a dedicated rotating log so normal logs do not grow without bounds.

### Phase 2: Capture the next incident

When the issue occurs:

1. Attempt one click and a short text entry in the affected field.
2. Record the approximate time.
3. Alt+Tab away and back once.
4. Preserve the Electron diagnostic log before restarting or updating the app.
5. Correlate the episode with Windows Application, System, WER, and watchdog
   records.

Classify the result:

- Heartbeat gap: renderer event loop or process stall.
- Heartbeat healthy, no pointer or keyboard events: native window activation or
  input routing failure.
- Pointer events arrive but focus does not change: Chromium focus or DOM issue.
- Focus changes and keyboard events arrive but text does not update: editor or
  page logic issue.
- Window becomes disabled or gains an owner: hidden native dialog.
- Immediate GPU-process exit or watchdog event: graphics process failure.

### Phase 3: Apply the narrowest fix

Choose a fix only after the diagnostic record identifies the failure mode.
Potential fixes, in preferred order:

1. Correct ownership and asynchronous handling for any native dialog that leaves
   the BrowserWindow inactive.
2. On genuine Electron activation loss, restore focus to the BrowserWindow and
   its previously focused WebContents without stealing focus during normal use.
3. Upgrade Electron if the captured signature matches a resolved Chromium or
   Electron focus defect.
4. Add guarded recovery for an unresponsive renderer or crashed GPU process.
5. Disable hardware acceleration only if repeated incidents correlate with GPU
   failures and an A/B run confirms that disabling it prevents recurrence.

Do not add timers that repeatedly call `focus()`, page-specific input hacks, or
automatic Alt+Tab equivalents. Those would hide the root cause and could steal
focus from other applications.

## Validation

- Verify focus, typing, modal dialogs, the find bar, multiple Sales Buddy
  windows, tray hide/show, startup minimized, and external-link navigation.
- Confirm diagnostics do not log typed text, note contents, customer data, or
  other sensitive values.
- Confirm diagnostic logging remains bounded during a multi-day run.
- Run the existing Electron build and shell validation commands.
- Rebuild and restage the Electron shell for a controlled production trial.
- Consider the issue resolved only after the user completes a representative
  trial period without another incident, or after a captured incident recovers
  through a targeted and explainable fix.

## Delivery note

Changes to `electron/main.js`, `electron/preload.js`, or the packaged Electron
version require an Electron shell rebuild. Any future changelog entry for the
implementation must use the `*Electron Shell Update* - ` marker.
