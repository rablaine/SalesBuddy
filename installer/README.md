# Sales Buddy MSI Installer

Builds a signed MSI installer for Sales Buddy using WiX v5 and C# custom actions (DTF).

## What the MSI Does

1. **Install**: C# custom actions handle:
   - Installing prerequisites via winget (Git, Python 3.13) if missing
   - Cloning the repo to `%LOCALAPPDATA%\SalesBuddy`
   - Creating a Python venv and installing pip dependencies
   - Building and staging the Electron desktop shell
   - Starting the supervisor and verifying the new web server and background worker

2. **Uninstall**: C# custom actions handle:
   - Stopping the server (kills waitress-serve and Python child processes)
   - Removing Start Menu and desktop shortcuts
   - Force-deleting app files (clears read-only git pack files, retries with delays)

3. **Existing-install upgrades**:
   - Use the common offline coordinator, never reset, clean, re-clone or recreate the runtime
   - Require the installed venv, dotenv, shared `app/db_paths.py`, Git and Node.js
   - Refuse dirty tracked files, invalid repositories, missing runtimes and non-fast-forward targets
   - Prove all installation processes have stopped before preparing a matched recovery point
   - Save the current code archive, runtime, desktop distribution and WAL-safe database snapshot
     outside the checkout; mirror the manifest in `data/upgrade-state.json`
   - Fast-forward only, install dependencies conditionally, rehearse schema changes on a database
     copy and apply the live schema only after rehearsal succeeds
   - Require a fresh startup-state boot identity and exact target commit, then run `wait-ready`
     and `accept` before displaying completion
   - Recover a failed update using the verified matched application/runtime/shell/database snapshot,
     while submitting sanitized diagnostics independently; no manual download is required

   The MSI embeds `scripts/upgrade.py` as an assembly resource and extracts an invocation runner
   under the installed checkout's `data` directory. It uses the existing venv and shared installed
   database resolver before changing application source. This also protects the first upgrade
   from an older version without its own coordinator. The invocation runner is removed afterward;
   the coordinator owns the durable recovery files.

   A damaged installation is **not** treated as a new install. Restore a known-good installation
   or use the saved recovery point instead of deleting files and trying again. A failed upgrade
   hands recovery to the saved runtime and coordinator, outside the installation being restored.
   The `recover --parent-pid PID --repo INSTALL_ROOT` operation waits for the launching parent,
   verifies and restores the matched snapshot, and opens an offline recovery notice.
   It is application-controlled recovery, not an MSI rollback action. The original
   sanitized report is durably queued and submitted before restoration. Once restored,
   Continue or closing the notice opens the workspace, even offline and without downloads.
   A separate optional contact request references the report using authenticated identity.
   Report export is optional; recovery failures still block the workspace.

   Restart enforcement applies when the restored version already includes upgrade safety.
   Installing this first safety version is a bootstrap exception requiring maintainer assistance
   if it fails. No persistent launcher, shortcut interception or older-version retrofit is added.

   Matched recovery covers application code, runtime, desktop files and database. It does **not**
   undo changes to external system Python or other prerequisites. Existing upgrades do not install
   prerequisites: repair missing system tools separately before retrying.

   First installs still bootstrap prerequisites, configuration and dependencies. A first-install
   desktop build may fall back to web wiring, but completion still requires the new supervisor,
   exact code revision and live worker to pass the same readiness check. Database initialization
   belongs to the supervisor's offline schema gate, not a full Flask factory inside the MSI.

4. **Custom UI**: Options dialog with checkboxes for:
   - Start Menu shortcut
   - Desktop shortcut
   - Auto-start on login
   - Launch Sales Buddy on exit

## Disposable-VM desktop recovery smoke test

Before packaging, run the offline historical migration checks from the repository:

```powershell
& .\venv\Scripts\Activate.ps1
pytest tests\test_historical_migration_hardening.py tests\test_upgrade_history.py tests\test_revenue_parent_migration.py tests\test_initiative_project_migration.py tests\test_task_hva_migration.py tests\test_migration_backups.py tests\test_database_startup.py
```

These tests use disposable synthetic SQLite databases, not the developer's or
installed user's database. They cover repeated real initialization, preservation of
old records/schema objects, rollback on invalid data, and a real process death
between dropping an original table and renaming its replacement. Rehearsal failure
tests invoke the actual private-copy updater path and require the source database
to remain byte-for-byte unchanged. Duplicate customer identities and conflicting
legacy selections remain blocked for review, not automatically deleted or reset.
This complements, rather than replaces, the packaged Windows recovery smoke test.
Migration snapshot writers share `app/services/migration_backups.py`: register new
backup labels there rather than creating independent timestamped snapshots.
Successful initialization prunes recognized loose migration archives to the two
newest verified copies per type. Failed initialization retains evidence, and
unchanged retries reuse an existing verified copy. Recovery-point retention remains
separate; manual, fiscal-year and scheduled backups are not migration archives.

For an unauthenticated fresh Windows VM, build an unsigned shell into a separate output folder,
then run the kit builder with that folder and the destination ZIP:

```powershell
& .\venv\Scripts\Activate.ps1
python .\scripts\build_upgrade_vm_kit.py --shell C:\temp\vm-shell\win-unpacked --output C:\temp\SalesBuddy-Upgrade-VM-Test.zip
```

The builder verifies the packaged shell against current source and bundles tracked working-tree
source plus the new recovery runtime files. It never includes the developer's environment,
database, virtual environment, Git directory or authentication cache.

Extract the kit on a disposable VM with a fresh normal installation, then use its five numbered
launchers. Setup creates synthetic records, bypasses first-run onboarding only in that VM and
redirects the VM installation's origin to a local test repository. The failed update rebuilds and
replaces the shell, commits a database canary, then fails at startup. Checks require proof that the
fault actually ran, exact desktop rollback, preserved notes and links, removal of the canary and
durable diagnostic queueing and notice dismissal. A second update tests successful replacement
and acceptance.
Public package downloads are required; corporate authentication is not.

After the successful-update check passes, `build_legacy_migration_addon(repo, original_kit, output)`
in `scripts/build_upgrade_vm_kit.py` packages a small add-on for that same VM. Extract it into
the original kit folder and use launchers 6, 7 and 8 without reinstalling or rerunning setup.
It backs up the synthetic database before adding a valid linked task whose legacy foreign key
still names the missing `_old_call_logs` table and whose milestone remains non-nullable.
The local candidate removes only the historical one-line parent-name repair. The real migration
must fail during rehearsal, before live migration or candidate startup. Verification requires
fresh failure evidence, unchanged legacy snapshots at failure, automatic diagnostic queueing
before dismissal, the restored good version, preserved notes/task links and a repaired schema.
A second desktop/worker boot must succeed without creating another recovery attempt.
This reproduces the known schema/code path, not a tester's private database. The add-on refuses
extra customer, note, milestone or task records and never publishes its deliberately broken commit.

Packaged-shell parity checks enumerate the actual bundled scripts and styles.
They must not read `package.json.build`, which electron-builder removes from packaged apps.
After a caller-owned migration launches Electron, Electron alone owns readiness and recovery.
The helper must not wait for another boot or overwrite an active/completed recovery.
Backend shutdown targets backend roles, not every Python process in the installation:
the update coordinator, repair process and diagnostic sender must survive shutdown.

Both migration launch paths and the shell-rebuild fallback use the coordinator's
`launch-desktop` action. On Windows, desktop/restored-workspace launches detach from
the helper console, start a new process group and break away from its job, with
stdin disconnected and output saved to `logs/workspace-launch.log`. Launch policy
failures are surfaced, never retried with an attached process. Closing an updater
terminal must not close the app or tray.
State publication retries only Windows access/sharing/lock failures during atomic
replacement, for at most two seconds. It preserves the prior complete state and
reports persistent failures rather than silently dropping an update.
Run `pytest tests\test_startup_lifecycle.py tests\test_upgrade_scripts.py` for the
native lock, actual console/job lifetime and launch-ownership regressions.

Windows managed children use CPython's own venv-aware direct-launch pattern, and Waitress runs
as a Python module. This preserves virtual-environment packages while ensuring the process ID
recorded by the supervisor is the actual process reporting web/worker health.

This exercises updates from the safety baseline, not MSI upgrade dialogs or the initial rollout
from an older shell. Do not run the kit on a real-user installation or publish its local commits.

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
| `SalesBuddy.CustomActions/UpgradeSafety.cs` | Guarded existing upgrades and fresh-boot acceptance |
| `SalesBuddy.CustomActions/PathHelper.cs` | PATH resolution and Python/Git discovery |
| `SalesBuddy.CustomActions/ProcessRunner.cs` | Process execution with MSI progress/status updates |
| `License.rtf` | MIT license shown on welcome page |
| `signing-metadata.json` | Azure Code Signing configuration |
| `build.ps1` | One-command build + sign script |
| `output/` | Build output (gitignored) |

## Upgrade validation

Source contract tests (no MSI runtime or real installed database required):

```powershell
pytest tests\test_installer_upgrade_safety.py
dotnet build installer\SalesBuddy.CustomActions\SalesBuddy.CustomActions.csproj -c Release
```

Windows MSI integration validation additionally needs a disposable installation with known data:
verify dirty-tree rejection, a missing runtime, a non-fast-forward target, shell build failure,
startup/worker failure, stale readiness rejection, successful acceptance, automatic recovery,
optional diagnostic-save cancellation, offline queueing and dismissal without downloads.
Do not run these destructive failure
scenarios against a user's working installation.
