# Plan: Diagnose Intermittent Electron Input Focus Loss

**Status:** Matching upstream Electron fix identified; controlled upgrade comparison pending
**Last updated:** 2026-10-22

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
- Ben reports that it happens more often while filling out **Create Task on
  Milestone**, after leaving the customer note's main text editor. Alt+Tab away
  and back restores typing in those episodes too.

## October 22 production incident findings

The tester exported `SalesBuddy-input-focus-2026-10-22T14-11-57-280Z.jsonl`
after adjusting a milestone task category, failing to focus the task title, and
recovering focus by switching away and back. Times below are UTC.

| Time | Captured evidence |
| --- | --- |
| 14:11:28 | The main window becomes disabled and loses foreground to another handle in the same Sales Buddy process, strongly suggesting a native modal dialog. |
| 14:11:32 | Windows foreground and Electron window/WebContents focus return, but the renderer reports `document.hasFocus() === false`. A roughly four-second renderer delay spans the interruption. |
| 14:11:34-44 | Pointer events reach enabled, writable text inputs, including `task_subject`. Active elements change without normal `focus`/`focusin` events. The task category select still emits input events. Subsequent renderer delays are only 0-8 ms, with no dropped diagnostic events. |
| 14:11:46-52 | Foreground moves to another process and back, consistent with the reported Alt+Tab recovery. Normal document and task-title focus events return. No subsequent text-entry attempt is captured before export. |
| 14:11:57 | The export incident marker is recorded after focus recovery. |

This is strong evidence of a desynchronized Electron/Chromium focus chain after
a native-dialog-like interruption, not a sustained renderer freeze or a disabled
task title. The capture contains no renderer/GPU crash or unresponsive event.
It does not identify the dialog, establish which keys were attempted, or prove
that a particular `alert()`/`confirm()` caller caused the failure.

The native probe's `ownerHandle` is the owner of the tracked main window, not
the owner of the foreground handle. A zero owner does not rule out an owned
dialog. Background-window timer throttling must not be classified as a stall.

### October 22 upstream fix discovery

Electron's [PR 54380](https://github.com/electron/electron/pull/54380), merged
September 26, addresses a closely matching Windows failure after JavaScript
`alert()`, `confirm()`, or a beforeunload prompt. The native window reactivates
before Chromium unblocks the renderer, leaving its native view focused but its
Blink widget blurred and its text-input/IME client detached. Deactivating and
reactivating the window restores input.

The upstream author reproduced `document.hasFocus() === false` after dismissal
and verified actual keyboard input on Windows 11. The fix reconciles the internal
focus state after the renderer is unblocked, only when those states disagree.
This is a much closer match to the captured foreground/WebContents/document
disagreement than a generic suggestion to call `focus()` from page JavaScript.

Verified release evidence:

- The captured shell and repository lockfile use **Electron 40.10.6**.
- [Electron 43.7.6 release notes](https://github.com/electron/electron/releases/tag/v43.7.6)
  explicitly include the repair through
  [PR 54462](https://github.com/electron/electron/pull/54462).
- [Electron 44.5.0 release notes](https://github.com/electron/electron/releases/tag/v44.5.0)
  explicitly include the same repair through
  [PR 54461](https://github.com/electron/electron/pull/54461).
- The original fix also has a merged 45.x backport. The initially closed
  backport attempts are not the merged backports.
- **43.8.3**, released October 21, is a concrete patched 43.x comparison target
  as of this investigation. Do not assume updating within 40.x supplies the fix.

The upstream symptom match is strong, but it does not prove which native
interruption occurred in the tester's session, or resolve the separate Save Note
failure. The next experiment should compare the existing runtime with a patched
runtime before adding more production instrumentation or application workarounds.

#### Controlled comparison

1. Use the existing live development backend and actual note editor in the
   development Electron shell, never the installed production backend. Preserve
   unsaved work and keep the app dark.
2. On 40.10.6, exercise a real native confirmation followed by Quill-to-task-title
   input, plus category-to-title input. Include both dismissal choices and an
   Alt+Tab while the native dialog is open. Do not create MSX tasks merely to test
   typing.
3. Record Windows activation, enabled state, WebContents focus, document focus,
   and whether actual keyboard input changes the field. Programmatic value
   assignment or a healthy browser-dev test is not a valid input test.
4. Update the Electron manifest and lockfile to an exact patched comparison
   version in a separate implementation step, run scoped shell tests, and rebuild
   the development shell. Repeat the same cases, including app-owned dialogs,
   Find, multiple windows, tray restore and external links.
5. If the old build fails and the patched build passes, use the runtime upgrade
   as the candidate fix and retain diagnostics for a representative tester trial.
   A shell rebuild/restage and an Electron Shell Update changelog marker are
   required; a backend update alone cannot ship this repair.
6. If the mismatch persists, or neither build reproduces it, retain the candidate
   status. Add privacy-safe dialog lifecycle/caller identification and foreground
   window class/ownership evidence for the next capture. The current native probe
   does not identify the foreground dialog or its owner.

### Separate Save Note failure and draft loss

The tester also reported that Save Note could not be activated after creating
the milestone task; Alt+Tab did not help. A second capture was reported but was
not available for this investigation. Treat that save failure as undiagnosed,
not automatically another instance of the captured focus mismatch.

Cancel then removed the note's recovery copy. At the time of the incident,
`cancelNote()` deliberately cleared the local draft and suppressed unload saving.
Commit `fc3f231c` introduced
this on March 23 to prevent stale drafts after cancelling an existing-note edit,
but applied it to new-note cancellation too. Independently created engagements
and MSX tasks can survive without a saved note, explaining an engagement with
zero associated notes without proving that a saved note was deleted.

The approved note lifecycle changes are now committed locally in `df5ad522`.
Close preserves meaningful unfinished work, explicit draft links open directly,
and Discard is separate from saving or leaving. Save acknowledgements and retry
protection reduce data-loss risk; they are not a fix for Electron focus loss.

### Next steps

1. Obtain the second capture and compare Save Note attempts with the focus
   signature above. Capture console errors, button/status state, and whether a
   note-save POST occurs, including its response. Do not export credentials or
   unredacted HAR files.
2. Run the controlled old-versus-patched Electron comparison above. If it does
   not explain the incident, identify the actual dialog caller with additional
   diagnostics. A healthy browser test cannot rule this out.
3. Keep the committed note lifecycle protection separate from focus recovery.
   Validate the runtime change without weakening safe saves or recovery.
4. Copy unsaved content if draft protection itself reports a failure. Close
   preserves drafts in the new UX; Discard intentionally removes them.
5. Do not add page-specific focus loops, automatic Alt+Tab equivalents, or disable
   GPU acceleration based on this capture.

## October 21 implementation checkpoint

- Phase 1 is implemented. The updated shell records a dedicated, bounded
  incident history without changing focus, input handling, or recovery behavior.
- The packaged development shell was built unsigned and exercised against the
  live port-5000 backend. Production installation/restaging is still pending.
- Phase 2 is next: capture an actual failure in a rebuilt production shell.
  A healthy smoke test does not establish the cause or resolve the original bug.
- Treat the note editor to milestone-task transition as the priority reproduction
  path, while retaining coverage for unrelated pages affected previously.
- Trace clicking out of Quill and, separately, pressing Escape to leave it.
  The note editor explicitly calls `quill.blur()` for Escape. This is a path to
  observe, not evidence that Quill causes the Electron-wide failure.
- Observe the task title (`task_subject`), description (`task_description`),
  category, and due date alongside the note editor. Record focus transitions,
  editable/enabled state, and whether input events reach each target, never
  field values, editor contents, or the actual keys typed.
- Do not treat a healthy browser-dev check as reproducing or ruling out the
  intermittent production Electron problem. Capture a failing shell episode
  before choosing a recovery change.

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

Previously deployed shells do not record enough focus or renderer state to
distinguish among the remaining causes. The new instrumentation requires a
shell rebuild before it can capture a production episode.

## Investigation plan

### Phase 1: Add production diagnostics (implemented)

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
- Whether the active element is contenteditable, disabled, or read-only, including
  transitions between Quill and the milestone task fields.
- `focus`, `blur`, `focusin`, `focusout`, `pointerdown`, and `keydown` events.
  Record event types and target metadata, not typed keys or input values.
- A lightweight heartbeat that records the largest event-loop delay.

Send diagnostic events to the main process through a narrowly scoped IPC
channel. Do not expose general IPC access to page JavaScript. Write diagnostics
to a dedicated rotating log so normal logs do not grow without bounds.

#### Implemented capture contract

- `electron/input-focus.js` owns window/app events, validated per-window IPC,
  native input-routing counts, incident snapshots, and log rotation.
- `electron/preload.js` captures only the top-level document. It buffers at most
  64 events and flushes at most once per second during normal activity.
  `keydown`, `beforeinput`, and `input` activity is aggregated by target/type,
  rather than logging each keystroke. An explicit incident snapshot can flush
  immediately. Dropped-event counts are retained if a burst exceeds the buffer.
- Renderer and main-process heartbeats are recorded every 15 seconds. The
  renderer samples its event loop every second and reports the largest delay.
  Visibility, minimized state, and the age of the last renderer batch accompany
  the evidence needed to distinguish an inactive page from a stalled one.
- The receiver accepts only the matching local main frame, limits each window
  to four batches per second, and reconstructs an allowlisted schema. Element
  IDs/names are retained only for fixed editor, task, find, and menu controls.
  Other controls still contribute tag/type and editable/enabled metadata.
- No typed keys, input data or values, note text, labels, customer names, page
  URLs, query strings, or external window titles are recorded. No diagnostics
  are uploaded automatically.
- On Windows, one hidden persistent PowerShell helper calls `GetForegroundWindow`,
  `IsWindowEnabled`, `GetWindow` (owner), and `GetWindowThreadProcessId`.
  Snapshots include request/capture timestamps, handles, and the foreground PID,
  not process names or titles. Native lookup runs asynchronously on focus/blur
  and incident capture, never synchronously on the Electron main thread.
- The helper is unpacked alongside the packaged ASAR so PowerShell can read it.
  Failed launches, invalid responses, timeouts, and process exits are reported;
  other diagnostic capture and the app continue without native snapshots.
- The active file is `logs/electron-input-focus.jsonl`, with
  `electron-input-focus.1.jsonl` and `electron-input-focus.2.jsonl` as archives.
  Each file is at most 2 MiB, for a 6 MiB rolling history. Oldest entries are
  overwritten, so preserve the log promptly after an incident.
- **Help > Save Input Focus Diagnostics...** marks the incident, requests fresh
  snapshots from every app window, and exports the retained files in time order
  to a user-selected destination. Save failures are surfaced explicitly.
  User-requested exports are separate from the rolling log.
- Window closure and app shutdown release listeners, timers, and the native
  helper. Page teardown stops renderer timers; history-cache restoration
  restarts capture without duplicate listeners.

### Phase 2: Capture the next incident

When the issue occurs:

1. Attempt one click and a short text entry in the affected field.
2. Record the approximate time.
3. Alt+Tab away and back once.
4. Choose **Help > Save Input Focus Diagnostics...** and save the export before
   restarting or updating. If the menu is also unresponsive, recover with Alt+Tab
   first. The retained history includes the activity preceding the export.
5. Correlate the episode with Windows Application, System, WER, and watchdog
   records.

Classify the result:

- Heartbeat gap while visible and active: possible renderer event loop or process
  stall. Hidden/minimized timer throttling or system sleep alone is not evidence
  of this bug.
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

Completed during implementation:

- Focused Node tests cover IPC trust/privacy, bounded buffering, input
  aggregation, loop delays, log rotation/export/failure, native probe
  failure/timeout, and lifecycle cleanup, alongside existing shell regressions.
  The startup pytest wrapper also runs the new diagnostic tests.
- An unsigned packaged build ran against the existing live development backend
  and data, using the separate dev-shell profile with dark mode unchanged.
- The live check exercised note-editor to task-title input, Escape from the
  editor followed by task-description input, an app modal, Find, additional
  windows, minimize/restore, Windows foreground/owner snapshots, and the real
  Help export handler. A known typed sentinel was absent from the export.
- Rotation limits and chronological export were verified using repeated writes;
  a representative multi-day production trial has not yet occurred.

Remaining release and incident checks:

- Verify focus, typing, modal dialogs, the find bar, multiple Sales Buddy
  windows, tray hide/show, startup minimized, and external-link navigation.
- Confirm diagnostics do not log typed text, note contents, customer data, or
  other sensitive values.
- Confirm diagnostic logging remains bounded during a multi-day run.
- Run the existing Electron build and shell validation commands.
- Restage the rebuilt Electron shell for a controlled production trial.
- Consider the issue resolved only after the user completes a representative
  trial period without another incident, or after a captured incident recovers
  through a targeted and explainable fix.

## Delivery note

Changes to `electron/main.js`, `electron/preload.js`, or the packaged Electron
version require an Electron shell rebuild. Any future changelog entry for the
implementation must use the `*Electron Shell Update* - ` marker.
