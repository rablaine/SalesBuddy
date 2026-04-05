# Sales Buddy MSI Installer

Builds a signed MSI installer for Sales Buddy using WiX v5 and C# custom actions (DTF).

## What the MSI Does

1. **Install**: C# custom actions handle:
   - Installing prerequisites via winget (Git, Python 3.13) if missing
   - Cloning the repo to `%LOCALAPPDATA%\SalesBuddy`
   - Creating a Python venv and installing pip dependencies
   - Running database migrations
   - Starting the server (waitress-serve)

2. **Uninstall**: C# custom actions handle:
   - Stopping the server (kills waitress-serve and Python child processes)
   - Removing Start Menu and desktop shortcuts
   - Force-deleting app files (clears read-only git pack files, retries with delays)

3. **Resilience** (handles dirty state from failed uninstalls):
   - Detects corrupted `.git` repos via `git rev-parse --verify HEAD`
   - Backs up `data/salesbuddy.db` to `%TEMP%` before nuking a broken install dir
   - Restores the database after a fresh clone so user data survives
   - Detects stale/broken venvs and recreates them
   - Always runs `pip install` even if venv already existed

4. **Custom UI**: Options dialog with checkboxes for:
   - Start Menu shortcut
   - Desktop shortcut
   - Auto-start on login
   - Launch Sales Buddy on exit

## Prerequisites for Building

- [.NET SDK 8.0+](https://dotnet.microsoft.com/download)
- WiX CLI tool:
  ```powershell
  dotnet tool install -g wix
  ```
- Azure Code Signing access (for `build.ps1` signing step)

## Build

```powershell
.\build.ps1
```

This builds the MSI and signs it with Azure Artifact Signing. Output: `output\SalesBuddy.msi`

To build without signing:

```powershell
dotnet build -c Release
```

## Project Structure

| File | Purpose |
|------|---------|
| `SalesBuddy.wixproj` | MSBuild project (references WiX SDK) |
| `Package.wxs` | Product metadata, features, component definitions |
| `install-actions.wxs` | Custom action declarations (Binary + CA entries) |
| `CustomUI.wxs` | Custom dialog set (Welcome, License, Options, Progress, Exit) |
| `SalesBuddy.CustomActions/` | C# DTF custom actions project (.NET 4.5.1) |
| `SalesBuddy.CustomActions/CustomActions.cs` | Install, uninstall, and server management logic |
| `SalesBuddy.CustomActions/PathHelper.cs` | PATH resolution and Python/Git discovery |
| `SalesBuddy.CustomActions/ProcessRunner.cs` | Process execution with MSI progress/status updates |
| `License.rtf` | MIT license shown on welcome page |
| `signing-metadata.json` | Azure Code Signing configuration |
| `build.ps1` | One-command build + sign script |
| `output/` | Build output (gitignored) |
