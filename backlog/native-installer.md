# Native MSI Installer with Live Progress

## Problem

The current MSI is a thin wrapper around `install.ps1`. It opens a PowerShell terminal window that runs for 8-12 minutes with scrolling text. Feedback from sellers:

1. **The terminal window is intimidating** - less technical sellers see a command prompt and think something went wrong or worry they'll break something
2. **It looks frozen** - the Azure CLI pip install takes 3-5 minutes with minimal output. Users think the installer crashed
3. **Checkbox options don't work** - the "Create desktop shortcut" and "Launch browser" checkboxes in the MSI finish page are cosmetic. `run-install.cmd` hardcodes `-Shortcuts -DesktopShortcut -LaunchBrowser` regardless of user selection

## Solution

Keep the WiX `.msi` format but replace the `.cmd` custom actions with **C# DTF custom action DLLs**. DTF (WiX's Deployment Tools Foundation) gives custom actions a `Session` object that can push real-time status text and progress bar updates to the MSI UI during installation.

No terminal window. No frozen progress bar. Still a `.msi`.

### How DTF custom actions update the UI

When a custom action is a C# DLL (not a .cmd/.exe), it runs inside the MSI engine process and gets a `Session` handle. That session can send messages back to the installer UI:

```csharp
// Update the status text shown on the progress page
using var record = new Record(0);
record[0] = "Installing azure-core 1.32.0 (45 of 127 packages)";
session.Message(InstallMessage.ActionData, record);
```

This works even during deferred custom actions - the key is the action runs in-process (DLL) rather than out-of-process (.cmd/.exe).

## Architecture

### Project structure

```
installer/
  SalesBuddy.CustomActions/          .NET class library (DTF custom action DLL)
    CustomActions.cs                  Entry points for each install step
    Services/
      PrerequisiteService.cs          Check/install winget, git, python, az, node
      AppSetupService.cs              Git clone, venv, pip, .env, migrations
      ShortcutService.cs              Start menu + desktop shortcuts
      ProcessRunner.cs                Run external commands, capture stdout
    SalesBuddy.CustomActions.csproj
  Package.wxs                         Main WiX package (updated)
  install-actions.wxs                  Custom action declarations (rewritten)
  CustomUI.wxs                        Custom dialog set with options page
  build.ps1                            Updated build script
```

### How it connects

1. `SalesBuddy.CustomActions.csproj` builds a `.CA.dll` (DTF packages the managed DLL + runtime into a native DLL that MSI can load)
2. `install-actions.wxs` declares each custom action pointing to methods in the DLL
3. `Package.wxs` references the `.CA.dll` as a Binary element
4. Each custom action method receives a `Session`, runs its step, and pushes status text + progress updates back to the UI

## Install Steps

Each step is a separate C# method decorated with `[CustomAction]`. The MSI sequences them in order. Between steps the MSI progress bar advances automatically. Within long steps (az CLI, pip install), the code pushes `ActionData` messages to update the status text live.

| Step | Method | Typical Duration | Live updates? |
|------|--------|------------------|---------------|
| 1. Check/install winget | `InstallWinget` | 5-30s | Status text |
| 2. Install Git | `InstallGit` | 1-2 min | Status text after completion |
| 3. Install Python | `InstallPython` | 30-60s | Status text |
| 4. Install Azure CLI | `InstallAzureCli` | 3-5 min | Per-package status text from pip stdout |
| 5. Install Node.js | `InstallNodeJs` | 20-40s | Status text |
| 6. Clone/update repo | `CloneRepository` | 15-45s | Status text |
| 7. Create venv + pip install | `SetupPythonEnv` | 1-2 min | Per-package status text from pip stdout |
| 8. Create .env + migrations | `ConfigureApp` | 2-5s | Status text |
| 9. Create shortcuts | `CreateShortcuts` | <1s | Status text |
| 10. Start server | `StartServer` | 1-2s | Status text |

### Status text the user sees

The MSI progress page shows a main text area that updates as each action runs:

```
Checking for winget...
Installing Git... this may take a minute or two
  Git installed successfully
Installing Python 3.13...
  Python installed successfully
Installing Azure CLI... this takes a few minutes
  Installing azure-core 1.32.0
  Installing azure-cli-core 2.68.0
  Installing azure-mgmt-resource 23.2.0
  Azure CLI installed successfully
Installing Node.js...
Cloning Sales Buddy repository...
Setting up Python environment...
  Installing Flask 3.1.0
  Installing SQLAlchemy 2.0.36
Running database setup...
Creating shortcuts...
Starting Sales Buddy server...
Installation complete!
```

### Skipping already-installed prerequisites

Same logic as current `install.ps1`: check if `git`, `python`, `az`, `node` are on PATH before installing. If present, push a status message "Git already installed, skipping..." and return immediately. The progress bar advances to the next step.

## ProcessRunner helper

Central class for running external commands (winget, git, pip, python) with stdout capture:

```csharp
public static int Run(string exe, string args, Session session, string statusPrefix)
{
    var psi = new ProcessStartInfo(exe, args)
    {
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        UseShellExecute = false,
        CreateNoWindow = true  // no terminal window
    };
    using var process = Process.Start(psi);
    while (!process.StandardOutput.EndOfStream)
    {
        var line = process.StandardOutput.ReadLine();
        if (!string.IsNullOrWhiteSpace(line))
        {
            // Push live text to the MSI UI
            using var record = new Record(0);
            record[0] = $"{statusPrefix} {line}";
            session.Message(InstallMessage.ActionData, record);
        }
    }
    process.WaitForExit();
    return process.ExitCode;
}
```

All prerequisite installs and pip steps route through this, so every external process gets live text output in the MSI progress page. `CreateNoWindow = true` means no terminal ever appears.

## Custom UI dialogs

### Options page

Replace `WixUI_Minimal` with a custom dialog set (`CustomUI.wxs`) that adds an options page between Welcome and Progress:

- **Checkbox: Create Start Menu shortcuts** (default: checked) - sets `STARTMENUSHORTCUT` property
- **Checkbox: Create desktop shortcut** (default: checked) - sets `DESKTOPSHORTCUT` property
- **Checkbox: Launch Sales Buddy when finished** (default: checked) - sets `LAUNCHBROWSER` property
- **Checkbox: Start automatically on login** (default: checked) - sets `AUTOSTART` property

Custom actions read these properties to decide what to do. Unlike the current MSI where `run-install.cmd` ignores the checkboxes, the C# code reads them directly from `session.CustomActionData`.

### Finish page

The standard WiX exit dialog with:
- Success message: "Sales Buddy has been installed successfully!"
- "Launch Sales Buddy" checkbox (bound to `LAUNCHBROWSER` property)
- Failure case: error message + "View Log" button that opens `%TEMP%\SalesBuddy-Install.log`

## Uninstall

Keep the existing approach: a deferred C# custom action `UninstallAction` that runs before `RemoveFiles`:

1. Stop the running server (find process on configured port)
2. Remove scheduled tasks (SalesBuddy-AutoStart, SalesBuddy-DailyBackup)
3. Remove shortcuts (Start Menu folder + desktop)
4. Backup database to `%TEMP%`
5. Remove app files (`%LOCALAPPDATA%\SalesBuddy`)

The MSI engine handles ARP removal and registry cleanup automatically.

## Build changes

Update `installer/build.ps1`:
1. Build the custom actions class library: `dotnet build installer/SalesBuddy.CustomActions -c Release`
2. Build the WiX MSI: `dotnet build installer -c Release` (WiX project references the CA DLL)
3. Copy MSI to `installer/output/`
4. Sign with Azure Artifact Signing (same as today)

## What we're replacing

| Current file | What happens to it |
|---|---|
| `scripts/install.ps1` | Logic ported to C# custom actions. PS1 kept for manual installs/dev use. |
| `scripts/run-install.cmd` | Deleted - no longer needed (no .cmd custom actions) |
| `scripts/run-uninstall.cmd` | Deleted - uninstall is now a C# custom action |
| `installer/install-actions.wxs` | Rewritten - points to C# DLL methods instead of .cmd files |
| `installer/Package.wxs` | Updated - new Binary element for CA DLL, custom UI reference, remove .cmd components |

## Implementation Phases

### Phase 1: Custom action DLL scaffold + first action
- Create `SalesBuddy.CustomActions` class library with DTF NuGet package
- Write `ProcessRunner` helper
- Port the simplest step (check/install winget) as a proof of concept
- Wire it into `install-actions.wxs` and verify the MSI builds
- Test: install the MSI, confirm no terminal window appears, confirm status text updates

### Phase 2: Port all prerequisite steps
- Port git, python, az CLI, node.js installation to C# custom actions
- Each as its own `[CustomAction]` method using `ProcessRunner`
- Verify live status text during az CLI install (the 3-5 min step)
- Skip logic for already-installed prereqs

### Phase 3: Port app setup steps
- Clone/update repo, create venv, pip install, .env, migrations, start server
- Shortcut creation (read MSI properties for user's checkbox selections)
- Auto-start scheduled task (conditional on checkbox)

### Phase 4: Custom UI + options page
- Build `CustomUI.wxs` with options dialog (checkboxes for shortcuts, launch, auto-start)
- Wire properties through to custom actions via `CustomActionData`
- Finish page with launch checkbox + error handling

### Phase 5: Polish + cleanup
- Update `build.ps1` for the new build flow
- Remove `run-install.cmd` and `run-uninstall.cmd`
- Update `Package.wxs` to remove .cmd file components
- Error handling, logging, retry logic for transient failures
- Code signing verification
- Test full install on a clean machine
