# What's New in Sales Buddy

Recent updates and improvements, newest first.

Each entry is tagged with the short SHA of the **merge commit** that
brought the change into `main`, so the admin Updates card can show
*exactly* which entries are pending vs already on your machine.
Format: `## M/D/YYYY - <merge-short-sha>`. See
`scripts/tag-changelog.ps1` for the helper that fills this in.

## 9/4/2026 - 2bd170a

- Make Engagement / Milestone Hygiene actionable with customer-scoped milestone linking, existing-or-new engagement remediation, and live summary counts.
- Add seller grouping, customer-aware ordering, highest-ACR default sorting, and clearer milestone details to Engagement / Milestone Hygiene.

## 9/1/2026 - 0bfd2a0

- Keep customerless CAIP milestones out of current-book milestone lists, dashboards, reports, calendars, and SalesIQ results.

## 9/1/2026 - 8160d1e

- Keep FY HoK Coverage focused on your current book by excluding milestones without an attached customer.

## 9/1/2026 - 6fb1b11

- Add CAIP Coverage to Activity Coverage with milestone-team Activities Logged and HoK metrics, automatic MSX evidence sync, and collapsible newest-first fiscal-year groups.

## 8/31/2026 - 16ca58f

- Automatically complete activities created from Activity Coverage, notes, and Fill My Day while retaining completed activity evidence and keeping the MSX Workspace focused on open tasks.

## 8/31/2026 - c7a2857

- Keep your place in Activity Coverage when dismissing meetings or creating standalone HoK tasks by updating affected rows without reloading the page.

## 8/31/2026 - d59b8e6

- Fix Activity Coverage so collapsing one meeting after Expand All leaves the other meetings expanded and keeps bulk controls synchronized.

## 8/31/2026 - 4777054

- Keep your place in Activity Coverage when creating or linking an activity by updating the meeting row without reloading the page.

## 8/31/2026 - 9b0c9f6

- Show open project tasks in the home-page Action Items card.

## 8/31/2026 - 670e407

- Add manual database compaction in Admin Tools to reclaim disk space left by deleted data, with before-and-after size reporting.
- Automatically compact the database after fiscal-year customer cleanup when at least 50 MB and 10% can be reclaimed, without failing a completed transition if maintenance is temporarily blocked.

## 8/28/2026 - b74f4b2

- Improve Activity Coverage editing with automatic draft saves, clearer customer and milestone selections, one-click clearing, live collapsed-row updates, and milestone results ranked by relevance.

## 8/28/2026 - 0c7e0d7

- Fix Ghost Aura meeting times, prefill new notes with the meeting time, and reliably cache up to 15 prioritized attendees plus the organizer for immediate import.

## 8/27/2026 - bce5ecb

- Make existing-activity matching easier to verify with a searchable picker showing activity date, milestone, activity type, and actual MSX creation date.
- Simplify Activity Coverage with a compact meeting filter, header expand control, continuous Full FY list, fewer toolbar actions, and remembered Meetings/Milestones and Weekly/Full FY preferences.

## 8/26/2026 - 62e598f

- Fix fiscal-year transitions stalling while backing up high-activity customers, show live customer backup progress, and report recoverable backup failures without suggesting the completed transition be rerun.

## 8/25/2026 - 0eeabf9

- Make fiscal-year calendar imports durable across restarts and run WorkIQ meeting queries in reliable five-day parallel batches, reducing week import time while preserving fast Outlook imports and per-day checkpoints.
- Treat empty WorkIQ process output as a retryable import failure instead of pausing with a `NoneType` error.

## 8/25/2026 - 701e2db

- Use configured corporate Outlook calendars for fast, deterministic historical meeting imports, with safe WorkIQ fallback when classic Outlook is unavailable or belongs only to a personal account.

## 8/25/2026 - 8d81100

- *Electron Shell Update* - Updating Sales Buddy now restores its window when finished instead of restarting silently in the system tray when Start minimized is enabled.

## 8/25/2026 - 25f6d44

- Add Activity Coverage to populate fiscal-year calendar history with resumable catch-up, refresh existing MSX activities, and create editable activities without leaving Sales Buddy.
- Add durable five-worker meeting preparation that stores WorkIQ summaries, drafts task details, prefers on-team milestone matches, and supports expand-all auditing with manual milestone overrides.
- Prefer HoK-credit activity types during meeting preparation, support safe preparation reruns, add Weekly and Full FY review modes, clarify calendar and MSX actions with contextual F1 guidance, and add searchable customer and milestone pickers with richer matching context.
- Prefer canonical Top accounts when duplicate MSX accounts share a TPID, repair stale customer account links during account sync, and refresh the local opportunity and milestone cache before Activity Coverage matching.
- Add milestone HoK coverage for active on-team milestones, with current-fiscal-year metrics, prior-year HoK context, covered/inactive filters, prepared-meeting handoff, and standalone HoK task creation.
- Automatically run the improved account sync once before Activity Coverage milestone matching when needed, ensuring the milestone refresh starts from current account assignments.

## 8/21/2026 - b3ab7f4

- Account sync now reads native MSX v-teams for accurate Growth and Acquisition sellers plus core solution engineers, rebuilds POD assignments from current account data, and supports both live progress and background operation.
- Account sync no longer imports CSAM or Digital Solution Specialist assignments.

## 8/20/2026 - 015f4df

- Revenue sync now uses the AzureBlue MSXI report, returning ACR for substantially more accounts and capturing more complete consumption for existing accounts.
- Revenue sync now retries transient Power BI TLS failures and removes repeated pagination boundary rows, preventing avoidable sync failures and duplicate-product errors.

## 8/18/2026 - 9881a07

- Fixed "Sync Accounts" failing with "No accounts found for this user." MSX stopped populating the account-team source we relied on, so account discovery now reads your account team memberships the same way MSX's own account team view does. Sync Now pulls your current accounts again.

## 8/10/2026 - fdb50fc

- *Electron Shell Update* - Fixed the in-app Update button failing with "Your local changes to the following files would be overwritten by merge" when the install directory had drifted (leftover edits from a half-finished prior update, CRLF churn, or stray build artifacts). Updates now reset the tree to the latest `origin/main` instead of refusing to pull, so the app updates cleanly every time.

## 8/6/2026 - d5452a6

- 🎉 **Revenue can now sync straight from MSXI - no CSV export.** 🎉 Open Import Revenue Data and click "Sync revenue now" to pull your accounts' ACR using your `az login`, with live progress. It also refreshes on its own in the background: the automatic milestone sync runs Monday, Wednesday, and Friday, and revenue rides along with it at most once every 7 days.
- The sync pulls 25 months of history (two full fiscal years plus the current one) and every bucket, rather than only the ones you picked in the export.
- Revenue rows now link to your customer records by TPID instead of guessing from the account name, so revenue lands on the right customer far more reliably.
- Sales Buddy now notices when MSXI renames or retires revenue buckets at the fiscal year boundary. If buckets you had selected disappear, it clears the selection, tells you which ones went away, and shows the new list to pick from. Review notes on surviving buckets are kept; the rest are saved to a JSON archive in your data folder before being removed.
- The setup wizard is down to two steps. Sign-in now sits on the welcome screen, and a single "Start Import" button pulls your accounts, milestones, and revenue in turn, each with its own progress bar, result summary, and retry.
- Removed the CSV revenue import. Everything now comes from the MSXI sync, so the upload form, the export instructions, and the hidden "Revenue Pull" beta page are gone. Revenue you imported previously is untouched.
- Removed the monthly "last month's revenue is finalized, go import it" banner. Sales Buddy pulls that for you now, so there is nothing to go do.
- Your import history is kept across syncs instead of being replaced each time, so you can still see every revenue import going back to your first one.
- The bucket-change notice now clears itself once you pick your buckets, rather than waiting for you to find the X.
- The revenue sync reports live progress as each batch of accounts comes back from MSXI, so the bar keeps moving through the long pulls instead of sitting still.

## 8/5/2026 - 5055be1

- *Electron Shell Update* - Added Find in Page to the desktop app. Press Ctrl+F (or Edit > Find in Page) to search the current page, cycle matches with Enter and Shift+Enter, and close with Esc. Matches are highlighted with the current one in orange, and a counter shows your position.

## 8/5/2026 - bc26b1b

- Added a hidden beta "Revenue Pull" page that fetches your Azure ACR directly from MSXI - fully headless using your `az login`, with no manual CSV export. It reports coverage stats and a per-account audit so we can validate it against full account lists before it replaces the manual revenue import.

## 8/4/2026 - 3696d7e

- Fixed daily automatic backups silently stopping. The recent WAL-safe backup change wasn't compatible with the way the scheduled task runs, so the backup failed every day without warning and no new files were saved. Backups now run correctly again.

## 8/3/2026 - 1cc45ad

- Fixed some imported meeting summaries still coming back blank. Certain WorkIQ responses were being misread as a different format, which dropped the whole summary. They now import fully every time.

## 8/3/2026 - 0b2e841

- Fixed meeting summaries imported from WorkIQ losing their formatting. Summaries now keep their paragraph breaks instead of collapsing into one block of text, no longer pull in stray meeting metadata, and reliably show their action items. Some imports had also started coming back with a blank summary, which is fixed too.

## 7/28/2026 - bd7246d

- Fixed pasting contact photos failing with "Error saving photo". A newer version of the image library (OpenCV 5) dropped the face-detection feature the app relied on, which broke photo saves. Photos now save reliably again, and the app automatically uses the compatible library version.

## 7/27/2026 - a2c9c7e

- Updated the revenue import instructions to point at the new **Azure Service Level Subscription Details SL4 - WW** MSXI report, and to check both **ServiceLevel2** and **ServiceLevel4** under Choose Fields when exporting your CSV.

## 7/27/2026 - 5c612ab

- Developer tooling: `scripts/dev.ps1` no longer leaks `AZURE_CONFIG_DIR` into the calling terminal. It now restores `FLASK_ENV` and `AZURE_CONFIG_DIR` to their prior values when the dev server exits, so later `az` commands in the same shell aren't silently redirected to the dev-isolated config dir.

## 7/27/2026 - 2d102fd

- The **Rebuild desktop app** button no longer hangs on older installs. If your installed app is too old to rebuild itself, you now get a clear message and a link to download the installer instead of a spinner that never finishes.

## 7/26/2026 - 631983a

- New **Start minimized** setting (Admin panel > Auto-Start, desktop app only). Turn it on and Sales Buddy starts quietly in the system tray when you log in instead of opening a window - your morning updates run in the background and the app opens instantly when you click the tray icon. Opening it from a desktop or Start Menu shortcut always shows the window.
- Installing or updating now warms the app up hidden in the tray during the final step, so when you click Finish the window pops up already loaded instead of cold-starting.
- Desktop-app updates can now finish themselves: when an update includes changes to the app shell, the Update button lets you know it'll take a little longer and rebuilds the desktop app automatically - no reinstall needed. A **Rebuild desktop app** button is also available under Admin > Danger Zone if you ever need to trigger it manually. (One-time note: your current app is too old to rebuild itself, so pick up this change by downloading the installer once from the [releases page](https://github.com/rablaine/SalesBuddy/releases/latest). Everything is automatic after that.)

## 7/26/2026 - dccf0ec

- Developer tooling: `scripts/dev.ps1` now accepts dash-prefixed action switches (`-Stop`, `-Start`, `-Restart`, `-Status`) alongside the positional form. Previously a token like `-Stop` was silently ignored and fell through to the default start action, so stopping the dev server actually relaunched it. Also gitignored the installation-specific `data-path.txt`.

## 7/26/2026 - 5badb46

- Added new Hands On Keyboard task types for FY27 (L300+ Demo, Rapid Prototyping, Solution Whiteboarding, and Technical Workshop). These now appear when creating MSX tasks and count toward your HoK credit. Also added the Assessment task category.

## 7/26/2026 - 0991553

- Fixed a problem that could block the desktop app from updating (a leftover build file made the update stop with an error). The app now installs its build dependencies in a way that leaves that file untouched, so updates apply cleanly.

## 7/26/2026 - 6fd6fa2

- Your automatic and manual backups are now taken with a consistent, crash-safe snapshot method, so a backup can never capture a half-written or stale copy of your database - even if you're actively using Sales Buddy the moment it runs.
- The daily backup no longer flashes a console window on screen at 11 AM. It now runs completely in the background.

## 7/26/2026 - 35dbe67

- Installing or updating the desktop app now opens Sales Buddy as soon as it's ready, instead of leaving you staring at a finished installer wondering if anything happened. The app starts warming up in the background during the last step of setup, so by the time you close the installer the window is already coming up.

## 7/26/2026 - 2929592

- The desktop app is now packaged with an updated, more secure build toolchain. This clears out the security warnings and "deprecated" notices you may have seen scroll by while installing or updating - including a critical advisory in the old packaging tools - and doesn't change how the app works. It's a behind-the-scenes cleanup of the install and update process.

## 7/25/2026 - 5d88a42

- Updating the desktop app is now much more resilient. If a previous version didn't shut down cleanly, Sales Buddy clears the leftover background processes on its own the next time it starts - so you no longer get stuck on "can't reach the server" after an update. And if an update can't run (for example, right after a fresh install before your system finishes setting up), the current version keeps running with a clear message instead of being left in a broken state.

## 7/24/2026 - 554128d

- Your database now lives in a protected location outside the app's install folder, so it can't be touched by an update, repair, or uninstall. When you first run this version your existing database is moved there automatically and safely - it's verified after the move, and the original is kept as a timestamped backup (never deleted).

## 7/24/2026 - 0b055e3

- Updating the desktop app no longer makes you quit it first. If Sales Buddy is open, the installer now asks for a quick OK to close it, then shuts it down for you and continues - instead of stopping with "quit from the tray and run setup again." Your data is protected the same as before.

## 7/24/2026 - 7808a21

- The desktop app now supports multiple windows. Open a fresh window from **File > New Window** (Ctrl+N) or the tray, and open any internal link in its own window with middle-click, Ctrl+click, or right-click "Open Link in New Window" - so you can keep a customer, your notes, and the calendar open side by side. Closing the last window tucks it back to the tray as before.
- Rows, tags, and calendar meetings are now real links: middle-click, Ctrl+click, or right-click to open a customer, note, territory, seller, POD, or a matched calendar meeting in its own window. Left-click still behaves exactly as before.

## 7/23/2026 - b8351d9

- Meeting sync now ignores internal "SME&C" segment meetings (kickoffs, co-sell connections, etc.) so they stop flooding your calendar and no longer get mis-matched to a customer named "SME". Existing ones are cleaned up automatically.
- Fixed customer name matching so generic words like "Corporate", "Properties", "Office", "Capital" and similar no longer single-handedly match a meeting to a customer (e.g. "Corporate Office Properties" was grabbing "FY27 Role Breakout | Corporate DSE"). Those customers still match on their email domain and full name.

## 7/23/2026 - c3b3ca7

- The desktop app now shows the proper Sales Buddy icon and name in the Windows taskbar - including when you pin it - instead of a generic "Electron" icon.
- Clicking around the desktop app (menus, dashboard links, opening an engagement) *should* now stay inside the app window instead of popping open your browser.
- Quitting from the system tray now shuts everything down cleanly, with no leftover background processes.
- Updating the desktop app is safer and more reliable: your database is protected across updates, and the install recovers cleanly even under a messy shutdown.

## 7/21/2026 - 9194b49

- New **Alignment Override** in Fiscal Year Management. When the new fiscal year starts but MSX hasn't updated your account-team assignments yet, you can pick the territories you're aligned to and have the account sync pull from those instead - so you're not stuck waiting on MSX to catch up. Everything downstream (milestones and the rest) follows your picks. Flip it off once MSX is updated and you're back to normal. Includes a one-click preview of how many customers it would pull before you commit.
- The fiscal-year "Finalize" now shows live progress while it purges last year's accounts, so that step no longer looks frozen on a large cleanup.
- Fixed the Fiscal Year Management card still nagging "It's transition time!" after you'd already completed this year's transition - it now stays quiet until next year.

## 7/20/2026 - 723291f

- When your Azure sign-in expires, Sales Buddy now catches it the moment an MSX or AI action fails and drops a banner at the top of the page with a one-click "Sign In to Azure" button - so you can re-authenticate right where you are instead of features quietly failing or having to dig into the admin panel. It works just like the "VPN required" banner, but for sign-in.

## 7/17/2026 - 03d822f

- Fixed the admin panel's Auto-Start status showing "Not registered" in the desktop app even when login autostart was actually set up. The desktop app registers autostart differently than the older browser install, and the admin panel was only checking the old spot. It now reads the right one, and the enable/disable toggle and Register button work in the desktop app too.

## 7/17/2026 - 29dd273

- Removed the old "install as an app" (PWA) support now that Sales Buddy has a real desktop app. This drops the service worker, web manifest, and offline page - which also clears out a class of stale-cache gremlins where an installed shortcut could show an old cached version. Use the desktop app (or just a browser tab) going forward.

## 7/17/2026 - 89c2ac6

- Off-VPN milestone syncs now bail in about a second. Before starting, the sync makes a single quick check that it can actually reach MSX - if you're off VPN/corpnet, it stops right away with the "connect to VPN and retry" banner instead of grinding through batch after batch for 15+ seconds first. Nothing gets touched.
- All MSX-backed syncs (milestones, marketing, and the rest) also bail out fast when a call confirms MSX is unreachable, instead of sitting through minutes of doomed retries.
- Fixed the admin "Update Now" button falsely reporting "update available" right after you clicked it. It now waits for the *new* version to actually come up before reloading, instead of latching onto the old server that's still shutting down.
- The desktop app is now officially version 1.0.

## 7/17/2026 - 6bbe2a9

- Fixed the milestone tracker's MSX sync misbehaving when you're off VPN: it used to churn through and report a scary number of milestones as "deactivated" (with error noise) even though nothing had actually changed. It now detects that it can't reach MSX, stops immediately, and shows a clear "connect to VPN and retry" message - without touching any of your milestones.

## 7/17/2026 - 40e10db

- Sales Buddy can now run as a desktop app - a real window with a system-tray icon that keeps your background tasks running - while still opening in your browser whenever you prefer. It has a proper app menu, back/forward navigation, and an About box.
- Added a "Move to desktop app" button in the admin Updates card so you can switch an existing install over to the desktop app in one click, without reinstalling.
- The desktop app can update itself: "Check for Updates" from the tray or menu pulls the latest version and restarts automatically, and it also checks quietly on launch.
- More reliable Azure sign-in. Fixed intermittent 401/403 sign-in failures caused by the Windows credential broker, and the app now records clear sign-in diagnostics so auth problems are easier to pin down.

## 7/16/2026 - 0d7527c

- Sales Buddy now runs under a supervisor that watches the web server and the background worker and automatically restarts either one if it crashes or hangs. If a background sync wedges or a process dies, it comes back on its own within seconds instead of silently staying down - so the app you rely on in the morning is far less likely to need a manual restart.

## 7/9/2026 - 7c07aa8

- Background jobs (MSX syncs, meeting prefetch, milestone updates, health checks) now run in a separate worker process from the web server. A slow or stuck background sync can no longer freeze or take down the app you're actively using - the web app stays responsive on its own, and it can now report whether the background worker is alive.

## 7/9/2026 - e205d2a

- Customer backups now save more reliably. The per-customer JSON files written to your OneDrive backup folder no longer get skipped when OneDrive briefly locks a file mid-sync (the write is retried), and any edit made right before you close the app is now flushed to backup instead of being lost.
- Added structured startup, shutdown, and crash logging so problems can actually be diagnosed. Sales Buddy records boot, clean-shutdown, and crash events to a log file and can now tell whether the previous run exited cleanly or was killed, instead of leaving you guessing.

## 7/8/2026 - f7bb47b

- Fixed the fiscal year "Finalize & Purge Orphans" step failing when any purged customer had contacts saved. The purge now removes a customer's contacts along with its notes, engagements, milestones, and opportunities, so finalization completes cleanly.

## 7/6/2026 - bfc866b

- Fixed WorkIQ-powered features (meeting attendee lookup, customer and partner contact scraping, daily meeting sync, and action-item suggestions) silently returning nothing. WorkIQ had started splitting some values across lines, which broke the response parsing. Sales Buddy now repairs those responses before reading them, so these features work again.

## 6/2/2026 - 852ce79

- Fixed a bug where Sales Buddy could lose its sign-in if you signed out of `az` in any other terminal, even though the app is supposed to keep its own separate session. Sales Buddy's session is now truly independent - signing in or out of `az` elsewhere no longer affects the app.

## 5/29/2026 - 9c53e89

- Fixed a bug where the server would silently lock up some time after auto-starting at login. The startup script was capturing the server's output through a pipe that nothing ever read, so once the OS pipe buffer filled, every waitress worker thread blocked on its next log write and the server stopped responding (manual `start.bat` launches were unaffected). The script now writes server output to a rotated log file at `%LOCALAPPDATA%\SalesBuddy\logs\server.log` (5 MB cap, one prior file kept), and no longer blocks on a "press any key" prompt when running headless under the scheduled task.

## 5/22/2026 - c72af75

- Updated auth system for better interoperability with operating-system-level `az login`. Sales Buddy now keeps its Azure CLI state in an isolated per-environment directory (`%USERPROFILE%\SalesBuddy\.azure` for production, `%USERPROFILE%\SalesBuddyDev\.azure` for development) so signing out of `az` in your normal terminal no longer logs the app out, and vice versa. Existing credentials are auto-migrated on first run - no re-authentication required.

## 5/7/2026 - 95517f9

- Improved the MSX Account Team endpoint probe. Probes now return in seconds instead of minutes when the endpoint is hung, classify timeouts as real failures (not VPN drops), and every real account-sync call to the same endpoint now contributes a free up/down signal to telemetry.

## 5/6/2026 - 3aee1d7

- Reworked the admin Updates card to be more resilient. The card now always shows the latest state when you open admin (no more "click Check Now to see what's actually there"), pulls the changelog directly from GitHub's API to avoid CDN staleness, and tracks the last deployed update in the database so "View last update" works across restarts, tabs, and browsers instead of only in the same tab that ran the update.

## 5/5/2026 - a0a20e4

- Added better WorkIQ tracking: every WorkIQ call now records whether it succeeded, the server was down, or the response failed to parse. This will give us a real picture of WorkIQ reliability over time.

## 5/5/2026 - 6506fe0

- Added background telemetry that probes the MSX Account Teams endpoint hourly so we can monitor when the recurring outage is happening.

## 5/5/2026 - d79313e

- "Check Now" in the admin Updates card now also refreshes the "View changelog" modal, so a forced re-fetch picks up brand-new entries in both places at once instead of just the card.

## 5/5/2026 - eb0c138

- Synapse Customers report polish: the Current tab now uses the most recent **complete** month for "Latest Month" and the 4-month average (so on May 5 it shows April's numbers, not May's partial data). The active tab is also remembered across visits via localStorage and applied before the page renders, so you no longer see the wrong tab flash on load.

## 5/5/2026 - b0e43c9

- Updated the MSX Account Teams API outage error message to ask users to send the error to Alex so he can engage the MSX team. The previous "try again in a few hours" wording implied the issue would self-resolve, which it won't.

## 5/5/2026 - 5bb2472

- Renamed the "New Synapse Customers" report to "Synapse Customers" with two tabs: **New** (customers who started using Azure Synapse Analytics in the last 6 months, same as before) and **Current** (every customer with any Synapse spend). The Current view replaces the "First Usage" column with an "Avg (Last 4mo)" column so you can spot drop-off, and customers within each seller group are sorted by latest month revenue descending. Old `/reports/new-synapse-users` URLs redirect to the new page.

## 5/1/2026 - 9c6efba

- Removed dead JS from the customer edit form that was throwing a non impacting error.

## 5/1/2026 - c5bb242

- Fixed admin "What just landed" sometimes coming up empty right after an update. The changelog used to be polled hourly in the background, which meant the first poll could grab a stale copy from GitHub's CDN seconds after a push and then sit on it for an hour. Now the changelog is lazy-loaded the first time you open the admin panel after boot, so what you see is always fresh.

## 5/1/2026 - 9902bb4

- Fixed ghost-aura retention so prefetched meetings stay around for 5 business days behind today instead of getting nuked the morning after the meeting. The home calendar will now actually show the trailing ghost aura it was always supposed to.
- Per-day calendar refresh spinners now persist while you navigate to other months and back, and the day auto-redraws with the new ghosts when the WorkIQ pull finishes (no more dead spinners or needing a hard refresh to see the result).
- Manual per-day refreshes no longer trigger the "purge expired ghosts" pass. That now only runs during the morning aura sync and the startup catchup, so clicking refresh on one day can't delete ghosts from other days.

## 4/30/2026 - 8f1ce35

- Fixed a bug in "What just landed" post update that caused it to sometimes not show the deployed changes.

## 4/30/2026 - 50807e7

- Changelog modal now shows the most recent 10 updates instead of the last 30 days, so a busy week doesn't flood it and a quiet stretch doesn't leave it empty.
- Activity and Action Items calendars on the home page now use a uniform cell height across every day (including weekends and out-of-month filler), so rows line up consistently no matter how full or empty a given day is.

## 4/30/2026 - 3ed0c48

- Improved WorkIQ meeting sync: fixed a timezone display bug and made the sync more resilient to flaky responses.

## 4/30/2026 - 8fe53c7

- Added a "View changelog" link in the admin Updates card header that opens a modal with the last 30 days of updates and a link to the full changelog on GitHub.

## 4/29/2026 - 19043e4

- Cache-bust the changelog fetch so the Updates card shows new entries right after a push instead of waiting for GitHub's CDN to expire.
- Restructure the changelog so each entry is tagged with the merge commit it covers, and the admin Updates card filters by commit hash instead of just date. This means multiple updates on the same day each get their own block, and you can see exactly which ones are pending vs already on your machine.

## 4/29/2026 - eb0ed08

- Rewrote milestone sync for a big performance boost (roughly 3-4x faster). Updated how we actually sync from MSX:
  - Opportunities: now scoped to the current Microsoft fiscal year through the next FY (a ~24-month window), plus any opp with no close date set. Previously we pulled all open opps regardless of close date but skipped recently-Won / Lost ones - now those come back too while their milestones still matter.
  - Stale milestone refresh: any local milestone that wasn't returned by the active sync (out-of-window, closed opp, etc.) is now refreshed in batches by milestone GUID directly, instead of round-tripping through the parent opportunity one at a time.
- Sync progress bar now reflects actual time spent per phase so it stops sitting at 82% for half the run.

## 4/28/2026 - 704abcc

- Added changelog viewer to admin Updates card so you can see what's new before and after applying an update.

## 4/28/2026 - 2cccf19

- Removed dead "Committed to bottom" toggle from U2C report (the toggle never did anything because committed and remaining milestones live in separate tables).

## 4/27/2026 - 65d2abb

- Fix Import Attendees getting stuck in "ready" mode after a failed scrape

## 4/27/2026 - 04c8d5e

- Stop milestone sync from re-marking completed milestones as stale

## 4/27/2026 - e1798fa

- Add DSS opportunity comment writeback option when creating notes.  Disabled by default, change in Settings.

## 2026-04-24

- Calendar columns now equal width with proper text truncation so long meeting titles don't break the layout

## 2026-04-23

- Add stale customer report broken down by territory and seller
- Show calendar sync icon when hovering a date so you can refresh just that day
- Improve meeting picker UX consistency and add ghost highlight styling
- Use the daily meeting cache for past-day meeting imports (faster, no live MSX call)
- Make scheduled task failures show up in the admin panel instead of silently failing
- Improve customer matching by trying the first token of the customer name
- Fix WorkIQ parser when meeting subjects contain pipe characters
- Fix a bug where ghost-aura sync was wiping calendar days

## 2026-04-22

- Add morning aura sync that prefetches the day's meetings on app start, with calendar dots showing which days have synced
- Add surgical per-day refresh so you can re-sync just one day from the calendar
- Note form now waits for ghost-aura before showing meeting picker so you don't see stale data
- Add WorkIQ failure telemetry to App Insights for diagnosing scrape issues
- Fix ANSI escape codes corrupting WorkIQ scrape output

## 2026-04-21

- Notes list now paginates so it loads fast even on customers with hundreds of notes
- Customer JSON backup now runs async, so saving a note no longer blocks for 20-60 seconds
- Fix SQLite lock contention during the async backup
- Auto-paste contact avatar after creating a new contact

## 2026-04-17

- Add offline page that explains what's happening when the network drops
- Daily 7AM meeting cache job (closes #120)
- MSI auto-launch and salesbuddy:// protocol handler

## 2026-04-16

- Batched milestone sync and improved opportunity sorting

## 2026-04-15

- WorkIQ status card is now draggable so you can move it out of the way

## 2026-04-14

- Use project title instead of topic name for general note calendar labels (cleaner display)
- Fix installer git detection so reinstalls work cleanly

## 2026-04-13

- Revenue import improvements (better matching, fewer false negatives)

## 2026-04-12

- Remove revenue engagements (replaced by direct revenue tracking)
- Revenue analyzer refinements
- Stale milestone improvements

## 2026-04-10

- Preserve milestone status during stale opportunity sync (no more accidental status drops)
- Fix Enter key in engagement contact dropdown
- Day-normalize MoM/CV calculations in revenue analysis for more accurate trend detection

## 2026-04-09

- Add Recently Viewed entities so you can jump back to where you were (closes #85)
- Fuzzy matching for customer domains
- Inline contact creation from forms (no more modal jump)
- New Action Items hub page
- SalesIQ MCP improvements: new tools, URL linking, milestone filters, system prompt hints

## 2026-04-08

- Action items now have due dates and a calendar tab (closes #116)
- Convert key individuals to engagement contacts so they get the full contact treatment (closes #114)
- Prefetch WorkIQ meetings on note page load for faster meeting picker (closes #117)
- Customer M&A handling: detect stale customers and provide a merge tool (closes #41)

## 2026-04-07

- Auto-add partner from attendees: partner contacts in a meeting auto-link their partner to the note
- Fix duplicate task creation when saving a note with an existing task linked
- Committed milestones no longer show overdue styling and countdown text
- Task improvements: modal chaining, workload filter, back-to-milestones button
- New Connect Impact report ranked by committed ACR/mo
- SalesIQ chat now renders markdown tables
- Split U2C% and Attainment% into separate cards for clarity
- Action item description now opens in a Quill rich-text flyout

## 2026-04-06

- Engagement AI fields, dynamic note form layout, related notes, engagement badges
- Inline editable engagement panels on note form (#113)
- Action item flyout description field with click-to-edit badges (closes #112)
- U2C snapshot report for quarterly milestone attainment tracking (closes #32)
- Add CSAM, DSS, and DAE stats to account sync summary
- Add Marketing Insights to Reports nav menu
- Marketing insights sync and report
- Table paste support in rich text fields

## 2026-04-05

- Fix PWA install card race condition that hid the install prompt
- Add Opportunities and Milestones to Browse nav menu
- MSX workspace report now has seller filter and calculated ACR

## 2026-04-04

- Native MSI installer v1.0.0 (resilient install/uninstall, idempotent)
- MCP server for VS Code Copilot integration

## 2026-04-03

- Switch AI to Azure Management JWT auth, removing the consent flow and ai_enabled toggle
- Add Internal Contacts (model, UI, MSX sync)
- Rename "call logs" to "notes" throughout the app
- Rename "Copilot" to "SalesIQ" throughout the app

## 2026-04-02

- SalesIQ Phase 4: tools registry, chat panel UI, chat endpoint with tool-calling
- Manual Milestone Sync button in admin panel (closes #105)
- Sign MSI with Azure Artifact Signing for trusted installs
- Fix attendee modal backdrop stacking on retry/cancel (closes #109)
- Partner scrape: append notes instead of replacing, detect WorkIQ server errors (closes #107)
- Detect SYSTEM-owned backup task in admin panel (closes #106)

## 2026-04-01

- Customer names in exports now link to TPID URL
- Remove Quick Actions panel from analytics (closes #93)
- Reports route cleanup with consistent header standardization (closes #94)
- AI partner recommendations for engagements
- Commitment status filter on milestone tracker (closes #90)
- Add help icon to navbar (closes #98)
- Configurable date range for What's New report
- Whitespace bucket drill-down (closes #92)
- Dismiss and "not useful" feedback on SalesIQ task suggestions (closes #96)
- Specialties selector keyboard behavior consistency (closes #102)
- Delay revenue import reminder to the 10th of the month
- Fix duplicate project dropdown and JS errors on general notes
- Fix territory-to-POD parsing for suffixed names (closes #104)
- Fix comment posting in milestone modal (closes #103)

## 2026-03-31

- Favorites for milestones, engagements, and opportunities (#86)
- Milestone tracker multiselect filters
- Milestone tracker "lost" hygiene status
- Workload report ACR deduplication
- Contact photo support
- Contact scraper for meeting attendees

## 2026-03-30

- New What's New report
- Make What's New collapsible
- Light mode visual improvements
- Milestone view shows all statuses
- Edit partner contacts inline
- Milestone audit trail (see who changed what when)
- Milestone team hint and on-team badge so you know which milestones are yours
- One-on-one report reorganized
- Fix PWA navigation and cancel-button referrer behavior
- Fix milestone dropdown overflow

## 2026-03-29

- Keyboard shortcuts throughout the app
- Branding update
- MSI installer fixes and OS theme detection
- Milestone calendar tabs
- Fix dirty working tree handling on reinstall
- Suppress git credential popups during update
- Fix NuGet Python PATH detection during install

## 2026-03-28

- New Whitespace report
- Milestone sync scheduler with MWF schedule and startup catchup
- Navbar customer search with `/` keyboard shortcut
- UX navigation overhaul
