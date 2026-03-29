# Desktop App Packaging - Build Plan

**Issue:** #53 - MSI Installer: Scheduled Task Startup for User-Friendly Local Deployment
**Spec:** `backlog/desktop-app-packaging.md`
**Branch:** `feature/msi-installer`

---

## Scope Summary

Build an MSI installer bootstrapper using WiX v4 that handles first-time setup for non-technical users. After install, the existing `git pull` / `update.bat` system handles all future updates. No code changes required, just packaging.

**What the MSI does:**
1. Installs prerequisites (Git, Python, Azure CLI, Node.js) via winget
2. Clones repo to `%LOCALAPPDATA%\SalesBuddy`
3. Creates venv, installs pip deps, generates `.env`
4. Runs `server.ps1` (migrations, scheduled tasks, server start)
5. Registers in Add/Remove Programs with clean uninstall
6. Final page checkboxes (all default checked):
   - "Launch Sales Buddy" - opens browser to `http://localhost:5151`
   - "Create Start Menu shortcuts" - Start Menu folder with app, start/stop/update links
   - "Create desktop shortcut" - desktop icon that opens browser to localhost

**Shortcut approach:** All shortcuts open `http://localhost:5151` via the default browser. If the user has installed the PWA, the browser automatically routes into the PWA window. If not, it opens as a normal tab. This means both web and PWA users get the right experience from the same shortcut with zero configuration.

**What already exists and we reuse:**
- `scripts/server.ps1` - prerequisite checks, venv setup, .env generation, scheduled tasks, server start
- `scripts/uninstall.ps1` - stops server, removes scheduled tasks
- `start.bat`, `stop.bat`, `update.bat` - existing bat launchers
- `scripts/run-hidden.vbs` - silent PowerShell launcher for scheduled tasks

---

## Phase 1: Bootstrap PowerShell Script

**Goal:** Create `scripts/install.ps1` - runs as the MSI custom action during the progress page. All output is silent (no terminal windows). The MSI progress bar shows status to the user.

**What it does (in order):**
1. Silent-install prereqs via `winget install --silent` (no prompts, no user choice - just install them all)
   - Git, Python 3.13+, Azure CLI, Node.js LTS
   - Skip any already installed (idempotent)
   - Refresh PATH after each install
2. `git clone https://github.com/rablaine/SalesBuddy.git` to `%LOCALAPPDATA%\SalesBuddy`
   - If dir already exists, skip clone (re-run safety)
3. Run `server.ps1` to complete setup:
   - Creates venv
   - Installs pip dependencies
   - Generates `.env` with random SECRET_KEY
   - Runs database migrations
   - Registers scheduled tasks (auto-start at login, daily backup)
   - Starts the server
4. Create shortcuts (based on flags passed from MSI checkbox properties):
   - Start Menu folder: Sales Buddy, Start Server, Stop Server, Update
   - Desktop shortcut: Sales Buddy
5. Report progress back to MSI UI at each step (via stdout/exit codes)

**Key decisions:**
- Script must be idempotent (safe to re-run if MSI install is retried)
- All winget installs use `--silent --accept-package-agreements --accept-source-agreements` (zero user interaction)
- No terminal windows appear - everything runs hidden behind the MSI progress page
- Use WshShell COM object for shortcut creation (standard Windows approach)
- Read PORT from `.env` after server.ps1 runs (defaults to 5151)
- Icon path on all shortcuts set to `static/icon.ico`

**Files created:**
- `scripts/install.ps1`

**Tests:** Manual only (PowerShell installer script)

---

## Phase 2: Uninstall Script Updates

**Goal:** Extend `scripts/uninstall.ps1` to also clean up shortcuts and optionally the app folder.

**What we add:**
1. Remove Start Menu folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Sales Buddy`)
2. Remove desktop shortcut (`%USERPROFILE%\Desktop\Sales Buddy.lnk`)
3. Prompt: "Delete app files and database?" (user choice)
   - If yes: remove `%LOCALAPPDATA%\SalesBuddy` entirely
   - If no: leave files (user can manually copy their database)
4. Keep existing behavior (stop server, remove scheduled tasks)

**Files modified:**
- `scripts/uninstall.ps1`

**Tests:** Manual only

---

## Phase 3: ICO File Generation

**Goal:** Create a proper `.ico` file from the existing SVG/PNG icon for use in shortcuts and Add/Remove Programs.

**What we do:**
1. Use Pillow to convert `static/icon-512.png` to `static/icon.ico` with multiple sizes (16, 32, 48, 64, 128, 256)
2. Commit the `.ico` file to the repo

**Files created:**
- `static/icon.ico`

---

## Phase 4: WiX v4 MSI Project

**Goal:** Create the WiX project files that build the MSI installer.

**Directory:** `installer/`

**What the MSI contains:**
- Product metadata (name, version, publisher, icon, upgrade GUID)
- Custom action: runs `scripts/install.ps1` (deferred, elevated if needed)
- Uninstall custom action: runs `scripts/uninstall.ps1 -Silent`
- Add/Remove Programs registration (icon, publisher, support URL)
- `InstallDir` property set to `%LOCALAPPDATA%\SalesBuddy`
- Upgrade table: detects previous MSI versions and removes them cleanly

**MSI UI flow (3 pages - standard WixUI_Minimal pattern):**
1. **Welcome page** - "Welcome to Sales Buddy Setup" with app icon. Single "Install" button. No options, no choices.
2. **Progress page** - "Installing Sales Buddy, please wait..." with a progress bar. Status text updates as each step completes:
   - "Installing Git..."
   - "Installing Python..."
   - "Installing Azure CLI..."
   - "Installing Node.js..."
   - "Downloading Sales Buddy..."
   - "Setting up environment..."
   - "Starting server..."
   - User cannot interact during this page - just watches the progress bar.
3. **Finish page** - "Sales Buddy is ready!" with three checkboxes (all default checked):
   - "Launch Sales Buddy" - opens browser to `http://localhost:5151`
   - "Create Start Menu shortcuts"
   - "Create desktop shortcut"
   - Finish button applies checkbox choices and closes the installer.

**Key WiX decisions:**
- WiX v4 (latest, uses .wixproj + .wxs XML format)
- Per-user install scope (no elevation for the MSI itself)
- winget calls inside install.ps1 may request elevation individually
- The MSI doesn't bundle Python/Git/Node - it downloads them via winget
- Version number tracks the MSI build, not the app (app updates via git)
- Final page has three checkboxes (all default checked):
  - "Launch Sales Buddy" - opens browser after install via WiX `LaunchApplication` custom action
  - "Create Start Menu shortcuts" - creates Start Menu folder with app/start/stop/update links
  - "Create desktop shortcut" - creates desktop `.lnk` to open browser
  - All three conditioned on checkbox properties, so install.ps1 receives them as flags

**Files created:**
- `installer/SalesBuddy.wixproj` - MSBuild project
- `installer/Package.wxs` - WiX package definition
- `installer/install-actions.wxs` - Custom action definitions
- `installer/README.md` - Build instructions

**Build command:**
```powershell
cd installer
dotnet build -c Release
# Output: installer/bin/Release/SalesBuddy.msi
```

**Prerequisites for building:**
- .NET SDK 8.0+
- WiX v4 (`dotnet tool install -g wix`)

---

## Phase 5: Build Script & CI

**Goal:** One-command MSI build for local dev.

**What we create:**
- `installer/build.ps1` - checks prerequisites, runs `dotnet build`, copies MSI to `installer/output/`
- Add `installer/output/` to `.gitignore`

**Files created:**
- `installer/build.ps1`

**Files modified:**
- `.gitignore` (add `installer/output/`)

---

## Phase 6: Documentation & Cleanup

**Goal:** Update docs for the new install flow.

**What we update:**
- `README.md` - add "Install" section with download link / build-from-source instructions
- Close issue #53 with a comment linking to the merge commit

**Files modified:**
- `README.md`

---

## Out of Scope (documented for future)

- **Code signing:** Need a code signing cert to avoid SmartScreen warnings. Track separately.
- **Release branch:** Currently users would track `main`. A `release` branch is a future consideration.
- **System tray app:** Issue #53 explicitly says "no system tray in this release."
- **Embedded Python (PyInstaller):** Full standalone packaging is v2+ per the spec.
- **WorkIQ global CLI install:** Nice-to-have from the spec, but separate from the MSI work. Can be a follow-up PR.
- **Existing install migration:** Auto-detecting `C:\dev\SalesBuddy` and offering to copy the DB. Nice-to-have for a follow-up.

---

## PWA Install Prompt (In-App)

Not part of the MSI build, but part of this feature branch since it completes the install experience.

**Where:** Step 1 of the new-user setup wizard (theme selection page).

**What:**
- After the theme picker, show a card explaining the PWA option:
  - "Want Sales Buddy as a desktop app? Click Install below for a standalone window with no browser tabs or address bar. You can always do this later from the install icon in your browser's URL bar."
  - "Install as Desktop App" button that triggers `beforeinstallprompt`
  - "No thanks, I'll use the browser" skip link
- If the browser doesn't support PWA install (no `beforeinstallprompt` event), hide the card entirely - don't confuse users with something they can't do.
- This is a soft offer, not a gate. Users proceed to step 2 regardless of their choice.

**Implementation:**
- Capture the `beforeinstallprompt` event in `base.html` JS and stash it globally
- In the wizard step 1 template, check if the event was captured and show/hide the PWA card
- "Install" button calls `deferredPrompt.prompt()` then awaits the user's choice
- Works in Edge and Chrome. Firefox/Safari won't fire the event, so the card stays hidden.

---

## Execution Order

1. **Phase 3** (ICO file) - quick, no dependencies
2. **Phase 1** (install.ps1) - core logic, testable standalone
3. **Phase 2** (uninstall.ps1 updates) - complement to Phase 1
4. **PWA prompt** - add `beforeinstallprompt` capture + wizard step 1 card
5. **Phase 4** (WiX MSI project) - depends on Phase 1-3
6. **Phase 5** (build script) - depends on Phase 4
7. **Phase 6** (docs) - last, after everything works

---

## Risk Notes

- **winget elevation:** Some winget installs (Git, Azure CLI) may require admin. The install script handles this by letting winget prompt for elevation per-package. The MSI itself stays per-user.
- **PATH refresh:** After winget installs, the current PowerShell session won't see new binaries until PATH is refreshed. The install script refreshes PATH after each install (same pattern as `server.ps1`).
- **Antivirus:** Unsigned MSIs and PowerShell scripts may trigger Windows Defender. Code signing (out of scope) would fix this.
- **WiX custom actions:** Running PowerShell from a WiX custom action has quirks (execution policy, working directory). The install script needs to be self-contained and handle its own error reporting.
