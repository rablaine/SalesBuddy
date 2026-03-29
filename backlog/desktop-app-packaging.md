# Packaging for Release - MSI Bootstrapper

## Goal
One-click MSI installer that sets up everything a non-technical user needs to run Sales Buddy. After install, the existing `git pull` update system stays in place - no need to ship new MSIs for code changes.

## Design Philosophy
The MSI is just a bootstrapper. It handles the painful first-time setup (prerequisites, cloning, venv, scheduled tasks) so the user never opens a terminal. After that, `update.bat` and the admin panel update button keep working exactly as they do today.

## What "Install" Means
1. Install prerequisites (Git, Python, Node.js, Azure CLI) via winget
2. Clone the repo to `%LOCALAPPDATA%\SalesBuddy`
3. Create venv, install pip dependencies
4. Generate `.env` with a random SECRET_KEY
5. Run `scripts\server.ps1` to handle migrations and scheduled task setup
6. Create shortcuts (desktop + Start Menu)
7. Launch browser to `http://localhost:5151`

After install, the server auto-starts at login via the existing `SalesBuddy-AutoStart` scheduled task. The desktop shortcut just opens the browser to localhost:5151.

## Install Location

**`%LOCALAPPDATA%\SalesBuddy`** (e.g. `C:\Users\jsmith\AppData\Local\SalesBuddy`)

NOT `C:\Program Files` - that's read-only without elevation, which breaks `git pull`, `pip install`, and writing to `data/`. The update system would require admin every time.

`%LOCALAPPDATA%` is user-writable, no elevation needed, and standard for per-user installed apps (VS Code, Chrome, Discord all install there).

## Shortcuts

### Desktop
- `Sales Buddy.lnk` -> opens default browser to `http://localhost:5151`
- Optional (prompt during install, don't force it)

### Start Menu
Create `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Sales Buddy\` with:
- `Sales Buddy.lnk` -> opens browser to `http://localhost:5151`
- `Start Server.lnk` -> `start.bat`
- `Stop Server.lnk` -> `stop.bat`
- `Update.lnk` -> `update.bat`

Start Menu folders still work fine on Windows 11 - they show up in search and the All Apps list.

## Prerequisites Installed by MSI

The installer checks for each and installs via winget if missing:

| Tool | Why | winget ID |
|------|-----|-----------|
| Git | Clone repo + pull updates | `Git.Git` |
| Python 3.13 | Flask backend | `Python.Python.3.13` |
| Node.js 18+ | WorkIQ CLI | `OpenJS.NodeJS.LTS` |
| Azure CLI | AI features + MSX auth | `Microsoft.AzureCLI` |

Each check is idempotent - if already installed, skip it. Same pattern as `server.ps1` but in the MSI custom action.

## Update Strategy

**No change from today.** The MSI installs a git clone. Updates happen via:
- `update.bat` (user-initiated, runs `git pull` + `pip install` + migrations)
- Admin panel "Check for Updates" button (same thing via the web UI)
- Both already work and are battle-tested

This is a huge advantage over Electron-style packaging where every code change requires building and shipping a new release. Here, you push to git and users pull when ready.

## MSI Build Approach

### Option A: WiX Toolset v4 (recommended)
WiX is the standard for Windows MSI creation. The MSI would contain:
- A custom action (PowerShell script) that runs the bootstrap sequence
- Shortcut definitions for desktop and Start Menu
- Add/Remove Programs registration
- An uninstaller that removes the app folder, shortcuts, and scheduled tasks

### Option B: NSIS
Simpler to set up than WiX, produces an .exe installer instead of .msi. Same capabilities. MSX Helper uses this via electron-builder, but we'd use it standalone.

### Recommendation
Start with WiX for a proper MSI. Corp machines often have policies that prefer .msi over .exe installers.

## MSI Custom Action - Bootstrap Script

The core of the installer is a PowerShell script (similar to `server.ps1`):

```powershell
# 1. Check and install prerequisites via winget
# 2. Refresh PATH after installs
# 3. git clone https://github.com/rablaine/SalesBuddy.git %LOCALAPPDATA%\SalesBuddy
# 4. cd %LOCALAPPDATA%\SalesBuddy
# 5. python -m venv venv
# 6. venv\Scripts\pip install -r requirements.txt
# 7. Copy .env.example to .env, generate SECRET_KEY
# 8. Run server.ps1 (handles migrations, scheduled tasks)
# 9. Create shortcuts
# 10. Open browser to http://localhost:5151
```

Much of this logic already exists in `server.ps1` - the MSI just front-loads the prerequisite installation.

## Uninstall

The MSI uninstaller should:
1. Run `stop.bat` to kill the server
2. Remove scheduled tasks (`SalesBuddy-AutoStart`, `SalesBuddy-DailyBackup`)
3. Remove Start Menu folder and desktop shortcut
4. Remove `%LOCALAPPDATA%\SalesBuddy` (code + venv)
5. **Keep** `data\salesbuddy.db` (prompt user: "Delete your data too?")

## Migration for Existing Users

Users currently running from source (e.g. `C:\dev\SalesBuddy`):
1. Install the MSI (creates new install at `%LOCALAPPDATA%\SalesBuddy`)
2. Copy `data\salesbuddy.db` from old location to new
3. Done - all data preserved

Could detect existing installs and offer to migrate automatically.

## Switch WorkIQ from npx to Global CLI Install

### Problem
We currently invoke WorkIQ via `npx -y @microsoft/workiq ask -q "..."` which:
- Has noticeable startup overhead on every call (npx checks the package cache)
- Auto-fetches the latest version silently, which could break things mid-day
- Requires more complex command construction (escaping, PowerShell wrapping)

### Proposed Change
Switch to `npm install -g @microsoft/workiq` so the `workiq` binary is on PATH directly. The MSI installer would run this as part of setup.

**In `query_workiq()` (`app/services/workiq_service.py`):**
1. Try `shutil.which('workiq')` first (global CLI install)
2. If not found, fall back to current npx approach (backward compatible)
3. When using the global CLI, invoke as: `workiq ask -q "..."`
4. EULA auto-accept becomes: `workiq accept-eula`

### Benefits
- **Faster:** No npx overhead on every meeting import / Fill My Day call
- **Predictable:** User controls when to update (`npm update -g @microsoft/workiq`)
- **Simpler commands:** `workiq ask` vs `npx -y @microsoft/workiq ask`

### Risk
- If global install path isn't on PATH, npx fallback kicks in automatically. Zero regression risk.

## Open Questions

- **Icon:** Need a proper `.ico` file for shortcuts and the Add/Remove Programs entry
- **Signing:** MSI should be code-signed to avoid SmartScreen warnings. Need a code signing cert.
- **PWA vs shortcut:** We already have a PWA manifest - should the desktop shortcut open the PWA install prompt instead of just the URL? PWA gives offline indicator and a proper app window.
- **Elevation:** winget installs may need admin. Does the MSI need to request elevation, or can we use per-user winget installs?
- **Branch:** Should end users track `main` or a `release` branch? A release branch would let you control exactly what users get.

## Future: Full Desktop Packaging (v2+)

The current backlog had a heavier approach using embedded Python + PyInstaller + Edge app mode. That's still a valid long-term goal for truly standalone distribution (no prerequisites at all), but it's much more work and gives up the `git pull` update flow. Consider this if:
- Distribution expands beyond Microsoft internal (can't assume winget)
- Users push back on having Git/Python/Node installed
- The update bat approach becomes too fragile

See git history for the original embedded-Python architecture plan.

## Priority
High - this is the main blocker for first release to colleagues
