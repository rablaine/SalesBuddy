# Sales Buddy MSI Installer

Builds an MSI installer for Sales Buddy using WiX v4.

## What the MSI Does

1. **Install**: Runs `scripts/install.ps1` which handles:
   - Installing prerequisites via winget (Git, Python, Azure CLI, Node.js)
   - Cloning the repo to `%LOCALAPPDATA%\SalesBuddy`
   - Setting up venv, pip dependencies, `.env` file
   - Running database migrations
   - Registering scheduled tasks (auto-start, daily backup)
   - Starting the server

2. **Uninstall**: Runs `scripts/uninstall.ps1 -Silent` which:
   - Stops the server
   - Removes scheduled tasks
   - Removes Start Menu and desktop shortcuts
   - Cleans up app files (preserves database to `%TEMP%`)

3. **Finish page**: Offers checkboxes to:
   - Launch Sales Buddy in the browser
   - Create Start Menu shortcuts
   - Create a desktop shortcut

## Prerequisites for Building

- [.NET SDK 8.0+](https://dotnet.microsoft.com/download)
- WiX v4 CLI tool:
  ```powershell
  dotnet tool install -g wix
  ```

## Build

```powershell
.\build.ps1
```

Or manually:

```powershell
dotnet build -c Release
```

Output: `output\SalesBuddy.msi`

## Project Structure

| File | Purpose |
|------|---------|
| `SalesBuddy.wixproj` | MSBuild project (references WiX SDK) |
| `Package.wxs` | Product metadata, UI, features |
| `install-actions.wxs` | Custom actions (install/uninstall PowerShell calls) |
| `License.rtf` | MIT license shown on welcome page |
| `Banner.bmp` | 493x58 top banner |
| `Dialog.bmp` | 493x312 welcome page background |
| `build.ps1` | One-command build script |
| `output/` | Build output (gitignored) |
