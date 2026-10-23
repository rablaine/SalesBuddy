using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using WixToolset.Dtf.WindowsInstaller;

namespace SalesBuddy.CustomActions
{
    /// <summary>
    /// WiX DTF custom actions for Sales Buddy installation and uninstallation.
    /// Each [CustomAction] method is an entry point callable from the MSI.
    /// All external processes run with CreateNoWindow=true (no terminal windows).
    /// Status text updates are pushed to the MSI progress page in real time.
    /// </summary>
    public partial class InstallerActions
    {
        // =====================================================================
        // Configuration
        // =====================================================================

        private const string RepoUrl = "https://github.com/rablaine/SalesBuddy.git";
        private const int DefaultPort = 5151;
        private const string AppName = "Sales Buddy";
        private const string PythonVersion = "3.13.2";
        private const string PythonNuGetUrl =
            "https://www.nuget.org/api/v2/package/python/" + PythonVersion;
        private const string GitVersion = "2.47.1";
        private const string GitPortableUrl =
            "https://github.com/git-for-windows/git/releases/download/v" + GitVersion
            + ".windows.1/PortableGit-" + GitVersion + "-64-bit.7z.exe";
        private const string NodeVersion = "v22.14.0";
        private const string NodeZipUrl =
            "https://nodejs.org/dist/" + NodeVersion + "/node-" + NodeVersion + "-win-x64.zip";

        // Step weights for progress bar (total = 98).
        // Calibrated from clean VM install log (2026-04-04, ~11.5 min total).
        private const int WeightWinget = 1;
        private const int WeightGit = 8;
        private const int WeightPython = 2;
        private const int WeightAzCli = 55;
        private const int WeightNode = 2;
        private const int WeightClone = 1;
        private const int WeightVenv = 21;
        private const int WeightConfig = 4;
        private const int WeightShortcuts = 1;
        private const int WeightAutoStart = 1;
        private const int WeightServer = 1;
        private const int WeightFinish = 1;

        // =====================================================================
        // Entry points
        // =====================================================================

        /// <summary>
        /// Main install action. Orchestrates all installation steps with
        /// live progress bar and status text updates in the MSI UI.
        /// Called as a deferred custom action after InstallFiles.
        /// </summary>
        [CustomAction]
        public static ActionResult InstallAction(Session session)
        {
            session.Log("=== Sales Buddy Installation Starting ===");

            // TLS 1.2 for all downloads (GitHub, NuGet, nodejs.org)
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;

            // Read properties passed via CustomActionData
            var data = session.CustomActionData;
            string installDir = data.ContainsKey("INSTALLFOLDER")
                ? data["INSTALLFOLDER"]
                : Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "SalesBuddy");

            bool startMenu = GetBoolData(data, "STARTMENUSHORTCUT");
            bool desktop = GetBoolData(data, "DESKTOPSHORTCUT");
            bool autoStart = GetBoolData(data, "AUTOSTART");
            string repoRef = GetRepoRef(data);

            session.Log($"Install directory: {installDir}");
            session.Log($"Options: StartMenu={startMenu}, Desktop={desktop}, " +
                        $"AutoStart={autoStart}");
            session.Log($"Repo ref: {repoRef}");

            // Damaged existing installs also require the guarded upgrade path.
            bool existingInstall = HasExistingInstallation(installDir);
            session.Log($"Existing install detected: {existingInstall}");

            // Pre-scan installed tools to rebalance the progress bar.
            // Steps that are already installed get weight=1 (brief blip),
            // so the bar's full range is distributed across actual work.
            PathHelper.RefreshPath();
            bool needsWinget = !PathHelper.FindWinget(session);
            bool needsGit = !PathHelper.FindGit(session);
            bool needsPython = !PathHelper.CommandExists("python");
            bool needsAzCli = !PathHelper.CommandExists("az");
            bool needsNode = !PathHelper.CommandExists("node");

            int wWinget = needsWinget ? WeightWinget : 1;
            int wGit = needsGit ? WeightGit : 1;
            int wPython = needsPython ? WeightPython : 1;
            int wAzCli = needsAzCli ? WeightAzCli : 1;
            int wNode = needsNode ? WeightNode : 1;
            int totalWeight = wWinget + wGit + wPython + wAzCli + wNode
                + WeightClone + WeightVenv + WeightConfig
                + WeightShortcuts + WeightAutoStart + WeightServer + WeightFinish;

            session.Log($"Pre-scan: winget={!needsWinget}, git={!needsGit}, " +
                        $"python={!needsPython}, az={!needsAzCli}, node={!needsNode}");
            session.Log($"Adjusted total weight: {totalWeight}");

            // Initialize progress bar with adjusted total
            InitProgress(session, totalWeight);

            // Send ActionStart so the UI knows how to render our status text.
            // Field 1 = action name, Field 2 = description, Field 3 = template.
            // The template "[1]" means ActionData field [1] shows as the detail line.
            using (var actionStart = new Record(3))
            {
                actionStart[1] = "InstallAction";
                actionStart[2] = "Installing Sales Buddy...";
                actionStart[3] = "[1]";
                session.Message(InstallMessage.ActionStart, actionStart);
            }

            try
            {
                // Existing installs must never enter the fresh-install bootstrap path.
                if (existingInstall)
                    return UpgradeExistingInstallation(
                        session, installDir, repoRef, startMenu, desktop, autoStart);

                // Refresh PATH so we can find already-installed tools
                PathHelper.RefreshPath();

                // Step 1: Winget
                ProcessRunner.UpdateStatus(session, "Checking for winget...");
                EnsureWinget(session);
                AdvanceProgress(session, wWinget);

                // Step 2: Git (~1 minute, synthetic progress)
                ProcessRunner.UpdateStatus(session,
                    needsGit ? "Installing Git... this typically takes about a minute"
                             : "Checking for Git...");
                if (needsGit)
                    RunWithSyntheticProgress(session,
                        () => EnsureGit(session),
                        wGit, targetSeconds: 60, reserveTicks: 1);
                else
                {
                    EnsureGit(session);
                    AdvanceProgress(session, wGit);
                }

                // Step 3: Python
                ProcessRunner.UpdateStatus(session, "Checking for Python...");
                EnsurePython(session);
                AdvanceProgress(session, wPython);

                // Step 4: Azure CLI (the big one - 5-7 minutes)
                // Uses synthetic progress: a background thread drips ticks
                // smoothly over 7 minutes while pip runs. When pip finishes,
                // remaining ticks are filled immediately.
                ProcessRunner.UpdateStatus(session,
                    needsAzCli ? "Installing Azure CLI... this typically takes 5-7 minutes"
                               : "Checking for Azure CLI...");
                EnsureAzureCliWithProgress(session, wAzCli);
                // Progress is fully handled inside EnsureAzureCliWithProgress

                // Step 5: Node.js
                ProcessRunner.UpdateStatus(session, "Checking for Node.js...");
                EnsureNodeJs(session);
                AdvanceProgress(session, wNode);

                // Verify critical commands before proceeding
                PathHelper.RefreshPath();
                PrependLocalTools();
                if (!PathHelper.FindGit(session))
                {
                    session.Log("FATAL: git not found on PATH after installation.");
                    return ActionResult.Failure;
                }
                if (!PathHelper.CommandExists("python"))
                {
                    session.Log("FATAL: python not found on PATH after installation.");
                    return ActionResult.Failure;
                }

                // Prove no installation process can race source/runtime setup.
                ProcessRunner.UpdateStatus(session, "Closing Sales Buddy if it's running...");
                StopRunningApp(session, installDir);

                // Step 6: Clone/update repository
                ProcessRunner.UpdateStatus(session, "Setting up Sales Buddy...");
                CloneOrUpdateRepo(session, installDir, repoRef);
                AdvanceProgress(session, WeightClone);

                // Step 7: Python environment (venv + pip install, ~2.5 min)
                ProcessRunner.UpdateStatus(session,
                    "Installing Python dependencies... this typically takes 2-3 minutes");
                RunWithSyntheticProgress(session,
                    () => SetupPythonEnv(session, installDir),
                    WeightVenv, targetSeconds: 150, reserveTicks: 2);

                // Step 8: Create first-install configuration.
                ProcessRunner.UpdateStatus(session, "Configuring application...");
                ConfigureApp(session, installDir);
                AdvanceProgress(session, WeightConfig);

                // Steps 9-11: Build and wire the Electron desktop shell. Delegates
                // to scripts/migrate-to-electron.ps1 so the MSI and the standalone
                // migration share one source of truth: it builds the shell locally
                // (Node is a prereq), swaps the shortcuts and the ON LOGON task to
                // Electron, and - on an existing Flask install - stops the old stack
                // first. Runs with -NoLaunch: we start the app ourselves at the end
                // of this action (background warm-up) and the Exit dialog launches
                // it again on Finish - the shell's single-instance lock dedupes them.
                ProcessRunner.UpdateStatus(session,
                    existingInstall ? "Switching Sales Buddy to the desktop app..."
                                    : "Building the desktop app... this may take a minute");
                bool electronOk = RunElectronSetup(session, installDir, startMenu, desktop, autoStart);
                AdvanceProgress(session, WeightShortcuts);

                if (!electronOk)
                {
                    // Fallback: keep the user working on the web/Flask stack if the
                    // desktop build failed for any reason.
                    session.Log("Electron setup failed; falling back to web wiring.");
                    if (startMenu || desktop)
                        CreateShortcuts(session, installDir, startMenu, desktop);
                    if (autoStart)
                        ConfigureAutoStartTask(session, installDir);
                }

                // Always register the daily backup task. It runs every day
                // regardless of the AUTOSTART option, and the backup script
                // gracefully no-ops if OneDrive hasn't been configured yet
                // (the user can configure it later from the admin panel).
                ConfigureBackupTask(session, installDir);
                AdvanceProgress(session, WeightAutoStart);
                AdvanceProgress(session, WeightServer);

                // Kick the desktop app off now, in the background, so its Python
                // supervisor cold-starts while the user reads the Exit dialog.
                // Otherwise the ~30s boot only begins on Finish, leaving a "did
                // it work?" gap after the user expects everything to be done.
                // The Exit dialog's LaunchApp still fires, but the shell holds a
                // single-instance lock, so that second launch just focuses the
                // already-starting window instead of spawning a duplicate. Safe
                // to do here: all staging/venv/config is finished, and the app's
                // data lives outside the install dir, so it can't race the MSI's
                // file commit. Only when the Electron shell staged cleanly - the
                // web fallback path already started its own server above.
                ProcessRunner.UpdateStatus(session, "Starting and verifying Sales Buddy...");
                using (var coordinator = new UpgradeCoordinator(installDir))
                    LaunchAndVerify(session, installDir, coordinator, electronOk, accept: false);

                // Step 12: Done
                AdvanceProgress(session, WeightFinish);

                ProcessRunner.UpdateStatus(session, "Installation complete!");
                session.Log("=== Sales Buddy Installation Complete ===");
                return ActionResult.Success;
            }
            catch (InstallCanceledException)
            {
                session.Log("Installation cancelled by user.");
                return ActionResult.UserExit;
            }
            catch (Exception ex)
            {
                session.Log($"FATAL: {ex}");
                ProcessRunner.UpdateStatus(session,
                    "Installation failed. Open Sales Buddy for startup diagnostics or use " +
                    "explicit recovery. Check the installer log; do not delete your installation.");
                return ActionResult.Failure;
            }
        }

        /// <summary>
        /// Uninstall action. Stops the server, removes scheduled tasks,
        /// shortcuts, and app files. Backs up the database first.
        /// Called as a deferred custom action before RemoveFiles.
        /// </summary>
        [CustomAction]
        public static ActionResult UninstallAction(Session session)
        {
            session.Log("=== Sales Buddy Uninstall Starting ===");

            var data = session.CustomActionData;
            string installDir = data.ContainsKey("INSTALLFOLDER")
                ? data["INSTALLFOLDER"]
                : Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "SalesBuddy");

            // When this uninstall is the "remove old product" half of a
            // MajorUpgrade, UPGRADINGPRODUCTCODE is set. In that case the newer
            // product is about to reinstall over this same dir, so we must NOT
            // delete it (esp. data/salesbuddy.db) - doing so is what wiped the
            // user's database on 2026-07-22.
            bool isUpgrade = data.ContainsKey("UPGRADINGPRODUCTCODE")
                && !string.IsNullOrEmpty(data["UPGRADINGPRODUCTCODE"]);
            session.Log($"Uninstall (isUpgrade={isUpgrade}).");

            try
            {
                // Stop the running server
                ProcessRunner.UpdateStatus(session, "Stopping Sales Buddy server...");
                StopServer(session, installDir);

                // Remove scheduled tasks
                ProcessRunner.UpdateStatus(session, "Removing scheduled tasks...");
                RemoveScheduledTasks(session);

                // Remove shortcuts
                ProcessRunner.UpdateStatus(session, "Removing shortcuts...");
                RemoveShortcuts(session);

                // Backup database (resolves to the external location once moved).
                string dbFile = ResolveDbPath(installDir);
                if (File.Exists(dbFile))
                {
                    string timestamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
                    string backup = Path.Combine(Path.GetTempPath(),
                        $"salesbuddy-uninstall-{timestamp}.db");
                    ProcessRunner.UpdateStatus(session, "Backing up database...");
                    File.Copy(dbFile, backup, true);
                    session.Log($"Database backed up to {backup}");
                }

                // Remove app files (force-delete with read-only clearing + retries).
                // Skipped during an upgrade - the new product reinstalls over this
                // dir and must keep the database + repo.
                if (isUpgrade)
                {
                    session.Log("Upgrade in progress - preserving install dir + database.");
                }
                else
                {
                    ProcessRunner.UpdateStatus(session, "Removing application files...");
                    if (Directory.Exists(installDir))
                    {
                        ForceDeleteDirectory(session, installDir);
                    }
                }

                ProcessRunner.UpdateStatus(session, "Uninstall complete.");
                session.Log("=== Sales Buddy Uninstall Complete ===");
                return ActionResult.Success;
            }
            catch (Exception ex)
            {
                session.Log($"Uninstall error: {ex}");
                return ActionResult.Success; // Don't block uninstall on errors
            }
        }

        // =====================================================================
        // Launch action (immediate, triggered from Exit dialog checkbox)
        // =====================================================================

        /// <summary>
        /// Launch Sales Buddy on finish. Prefers the Electron desktop shell; if
        /// it isn't present (e.g. the desktop build fell back to web wiring),
        /// opens the web UI in the default browser instead.
        /// </summary>
        [CustomAction]
        public static ActionResult LaunchApp(Session session)
        {
            try
            {
                string installDir = session["INSTALLFOLDER"];
                if (string.IsNullOrEmpty(installDir))
                {
                    installDir = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                        "SalesBuddy");
                }

                LaunchDesktopApp(session, installDir);
            }
            catch (Exception ex)
            {
                session.Log($"LaunchApp error (non-fatal): {ex.Message}");
            }

            return ActionResult.Success;
        }

        /// <summary>
        /// Launch the staged desktop shell if present, else fall back to the web
        /// UI in the browser. Safe to call more than once: the Electron shell
        /// holds a single-instance lock, so a second launch just focuses the
        /// existing window rather than spawning a duplicate. Used both by the
        /// end-of-install warm-up and the Exit dialog's LaunchApp action.
        ///
        /// <paramref name="args"/> lets the warm-up pass "--startup --minimized"
        /// so the shell boots hidden to the tray; the Exit dialog's Finish launch
        /// passes none, which fires the shell's second-instance handler and pops
        /// the (already warmed) window up.
        /// </summary>
        private static void LaunchDesktopApp(Session session, string installDir, string args = "")
        {
            string exe = Path.Combine(installDir, "electron-dist", "Sales Buddy.exe");
            if (File.Exists(exe))
            {
                session.Log($"Launching desktop app: {exe} {args}".TrimEnd());
                if (string.IsNullOrEmpty(args))
                    Process.Start(exe);
                else
                    Process.Start(exe, args);
            }
            else
            {
                int port = GetPortFromEnv(installDir);
                string url = $"http://localhost:{port}";
                session.Log($"Desktop exe not found; launching browser: {url}");
                Process.Start("explorer.exe", url);
            }
        }

        /// <summary>
        /// Immediate custom action - runs in the UI phase as the interactive user,
        /// early (before LaunchConditions), so it reliably sees the user's GUI
        /// processes (unlike the deferred InstallAction). A running app holds file
        /// locks that make the install corrupt itself (half-staged shell +
        /// dependency-less venv), so instead of blocking, we CLOSE it for the user
        /// (a graceful shutdown request, then a force-kill fallback) so they don't
        /// have to Quit from the tray first. SALESBUDDYRUNNING is only set if the app
        /// somehow can't be closed, leaving the LaunchCondition to block as a last
        /// resort. Never throws - on any error it reports "not running" so a
        /// detection hiccup can't wedge the installer.
        /// </summary>
        [CustomAction]
        public static ActionResult CheckAppRunning(Session session)
        {
            string installDir = session["INSTALLFOLDER"];
            if (string.IsNullOrEmpty(installDir))
                installDir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "SalesBuddy");

            bool running = IsShellRunning(session);

            // Close the app for the user rather than blocking the install and making
            // them manually Quit from the tray: graceful shutdown request first, then
            // a force-kill fallback that works for any prior shell version. Only if
            // it STILL won't die do we leave SALESBUDDYRUNNING set for the gate.
            if (running)
            {
                // Interactive installs: ask before closing the user's app. Silent /
                // automated installs (no full UI) just close it - no one's there to
                // answer a prompt.
                if ((session["UILevel"] ?? "") == "5" && PromptCloseApp(session) == MessageResult.Cancel)
                {
                    session.Log("CheckAppRunning: user cancelled at the close prompt - aborting install.");
                    return ActionResult.UserExit;
                }
                session.Log("CheckAppRunning: closing the running shell so the install can proceed.");
                try { CloseRunningApp(session, installDir); }
                catch (Exception ex) { session.Log($"CheckAppRunning: close error (non-fatal): {ex.Message}"); }
                running = IsShellRunning(session);
            }

            session["SALESBUDDYRUNNING"] = running ? "1" : "";
            session.Log($"CheckAppRunning: SALESBUDDYRUNNING='{session["SALESBUDDYRUNNING"]}'");

            // With the shell down (never up, or we just closed it), sweep any stray
            // backend (waitress / worker) this install leaked. A pre-1.1.9 tray Quit
            // killed the shell but left the backend alive, holding the DB open and
            // port 5151. Kill them HERE - an immediate CA in the UI phase, BEFORE
            // RemoveExistingProducts runs the old uninstall's DB backup + delete - so
            // a leftover worker can't produce an inconsistent DB backup or lock files
            // into a corrupt install. Skipped only if the app somehow survived the
            // close above (a live supervisor would just respawn the backend). Never
            // throws.
            if (!running)
            {
                try
                {
                    StopBackendProcesses(session, installDir);
                }
                catch (Exception ex)
                {
                    session.Log($"CheckAppRunning: backend sweep error (non-fatal): {ex.Message}");
                }
            }
            return ActionResult.Success;
        }

        // =====================================================================
        // Progress helpers
        // =====================================================================

        /// <summary>
        /// Tell the MSI engine how many progress ticks our custom action will report.
        /// Must be called once at the start before any AdvanceProgress calls.
        /// </summary>
        // We scale ticks so bar movement is visible.
        private const int TickScale = 500;

        private static void InitProgress(Session session, int totalTicks)
        {
            // Type 0 = Reset. Takes over the progress bar so our custom
            // action controls 0-100%.
            using (var record = new Record(4))
            {
                record[1] = 0; // Reset
                record[2] = totalTicks * TickScale;
                record[3] = 0; // Forward direction
                record[4] = 0; // Execution phase
                session.Message(InstallMessage.Progress, record);
            }

            // Establish progress context so Type 2 increments work immediately.
            using (var actionInfo = new Record(4))
            {
                actionInfo[1] = 1; // Type 1 = action info
                actionInfo[2] = 0; // 0 ticks per ActionData (we advance manually)
                actionInfo[3] = 0;
                actionInfo[4] = 0;
                session.Message(InstallMessage.Progress, actionInfo);
            }
        }

        /// <summary>
        /// Advance the MSI progress bar by the specified number of ticks.
        /// </summary>
        private static void AdvanceProgress(Session session, int ticks)
        {
            using (var record = new Record(2))
            {
                record[1] = 2; // Type 2 = increment
                record[2] = ticks * TickScale;
                session.Message(InstallMessage.Progress, record);
            }
        }

        /// <summary>
        /// Run an action with a background thread that smoothly advances
        /// the progress bar over [targetSeconds]. When the action finishes,
        /// the drip stops and remaining ticks fill immediately.
        /// </summary>
        /// <param name="session">MSI session.</param>
        /// <param name="action">The work to perform (runs on main thread).</param>
        /// <param name="weight">Total ticks allocated for this step.</param>
        /// <param name="targetSeconds">Expected duration to spread ticks over.</param>
        /// <param name="reserveTicks">Ticks to hold back for the completion bump.</param>
        private static void RunWithSyntheticProgress(
            Session session, Action action, int weight,
            int targetSeconds, int reserveTicks)
        {
            int syntheticTicks = weight - reserveTicks;
            if (syntheticTicks <= 0)
            {
                action();
                AdvanceProgress(session, weight);
                return;
            }

            int intervalMs = (targetSeconds * 1000) / syntheticTicks;
            int ticksDripped = 0;
            var done = new ManualResetEventSlim(false);

            var drip = new Thread(() =>
            {
                while (ticksDripped < syntheticTicks)
                {
                    if (done.Wait(intervalMs))
                        break;
                    ticksDripped++;
                    AdvanceProgress(session, 1);
                }
            });
            drip.IsBackground = true;
            drip.Start();

            action();

            done.Set();
            drip.Join(5000);

            int remaining = weight - ticksDripped;
            if (remaining > 0)
                AdvanceProgress(session, remaining);
        }

        // =====================================================================
        // Prerequisite steps
        // =====================================================================

        /// <summary>
        /// Ensure winget is available. If not found, install it from GitHub.
        /// </summary>
        private static void EnsureWinget(Session session)
        {
            if (PathHelper.FindWinget(session))
            {
                session.Log("winget already available.");
                ProcessRunner.UpdateStatus(session, "winget found, skipping...");
                return;
            }

            session.Log("winget not found. Installing from GitHub...");
            ProcessRunner.UpdateStatus(session, "Installing winget...");

            // The winget bootstrap requires Add-AppxPackage (PowerShell cmdlet).
            // We run it as a hidden PowerShell process - no terminal window.
            string script = @"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Stop'

# VCLibs dependency
$vcLibsUrl = 'https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx'
$vcLibsPath = Join-Path $env:TEMP 'VCLibs.appx'
Write-Host 'Downloading VCLibs...'
Invoke-WebRequest -Uri $vcLibsUrl -OutFile $vcLibsPath -UseBasicParsing
Add-AppxPackage -Path $vcLibsPath -ErrorAction SilentlyContinue

# UI.Xaml from NuGet
$xamlUrl = 'https://www.nuget.org/api/v2/package/Microsoft.UI.Xaml/2.8.6'
$xamlZip = Join-Path $env:TEMP 'UIXaml.nupkg.zip'
$xamlDir = Join-Path $env:TEMP 'UIXaml-nupkg'
Write-Host 'Downloading UI.Xaml...'
Invoke-WebRequest -Uri $xamlUrl -OutFile $xamlZip -UseBasicParsing
if (Test-Path $xamlDir) { Remove-Item $xamlDir -Recurse -Force }
Expand-Archive -Path $xamlZip -DestinationPath $xamlDir -Force
$appx = Join-Path $xamlDir 'tools\AppX\x64\Release\Microsoft.UI.Xaml.2.8.appx'
if (Test-Path $appx) { Add-AppxPackage -Path $appx -ErrorAction SilentlyContinue }

# winget from GitHub
Write-Host 'Downloading winget...'
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/winget-cli/releases/latest' -UseBasicParsing
$msixUrl = ($release.assets | Where-Object { $_.name -match '\.msixbundle$' }).browser_download_url
$licUrl = ($release.assets | Where-Object { $_.name -match 'License.*\.xml$' }).browser_download_url
$msixPath = Join-Path $env:TEMP 'winget.msixbundle'
Invoke-WebRequest -Uri $msixUrl -OutFile $msixPath -UseBasicParsing

if ($licUrl) {
    $licPath = Join-Path $env:TEMP 'winget-license.xml'
    Invoke-WebRequest -Uri $licUrl -OutFile $licPath -UseBasicParsing
    try {
        Add-AppxProvisionedPackage -Online -PackagePath $msixPath -LicensePath $licPath -ErrorAction Stop
        Write-Host 'winget provisioned system-wide.'
    } catch {
        Write-Host 'Provisioned install failed (expected if not admin). Falling back...'
    }
}
Add-AppxPackage -Path $msixPath -ErrorAction SilentlyContinue

# Ensure WindowsApps is on PATH
$wa = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
if ($env:Path -notlike ""*$wa*"") { $env:Path += "";$wa"" }
Write-Host 'winget installation complete.'
";

            int exitCode = ProcessRunner.RunPowerShell(session, script);
            PathHelper.RefreshPath();

            // Add WindowsApps to our process PATH
            var windowsApps = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Microsoft", "WindowsApps");
            PathHelper.AddToPath(windowsApps);

            if (PathHelper.FindWinget(session))
            {
                session.Log("winget installed successfully.");
            }
            else
            {
                session.Log("winget installation completed but command not found. Continuing...");
            }
        }

        /// <summary>
        /// Ensure Git is installed. Uses FindGit to check common locations,
        /// tries winget first, then falls back to portable Git zip.
        /// </summary>
        private static void EnsureGit(Session session)
        {
            PathHelper.RefreshPath();
            if (PathHelper.FindGit(session))
            {
                session.Log("Git already installed.");
                ProcessRunner.UpdateStatus(session, "Git already installed, skipping...");
                return;
            }

            // Try winget first
            if (PathHelper.CommandExists("winget"))
            {
                ProcessRunner.UpdateStatus(session,
                    "Installing Git... this typically takes about a minute");
                InstallViaWinget(session, "Git", "Git.Git", "git");

                PathHelper.RefreshPath();
                if (PathHelper.FindGit(session))
                    return;

                session.Log("winget Git install did not produce a usable git. " +
                            "Falling back to portable Git.");
            }

            // Fallback: portable Git (self-extracting archive, no MSI/installer conflict)
            InstallPortableGit(session);
        }

        /// <summary>
        /// Install portable Git to %LOCALAPPDATA%\git. Self-extracting 7z archive
        /// avoids MSI mutex conflicts and works without admin.
        /// </summary>
        private static void InstallPortableGit(Session session)
        {
            ProcessRunner.UpdateStatus(session, "Installing portable Git...");

            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            var gitDir = Path.Combine(localAppData, "git");
            var tempExe = Path.Combine(Path.GetTempPath(),
                $"PortableGit-{GitVersion}.exe");

            try
            {
                ProcessRunner.UpdateStatus(session, "Downloading portable Git...");
                DownloadFile(session, GitPortableUrl, tempExe);

                ProcessRunner.UpdateStatus(session, "Extracting portable Git...");
                if (Directory.Exists(gitDir))
                    Directory.Delete(gitDir, true);
                Directory.CreateDirectory(gitDir);

                // PortableGit .7z.exe is a self-extracting archive.
                // -o = output dir, -y = yes to all
                int exitCode = ProcessRunner.Run(session, tempExe,
                    $"-o\"{gitDir}\" -y");

                if (exitCode != 0)
                {
                    session.Log($"Portable Git extraction failed (exit {exitCode}).");
                    return;
                }

                var cmdDir = Path.Combine(gitDir, "cmd");
                if (File.Exists(Path.Combine(cmdDir, "git.exe")))
                {
                    PathHelper.AddToPath(cmdDir, persist: true);
                    session.Log($"Portable Git {GitVersion} installed to {gitDir}.");
                }
                else
                {
                    // Some versions put git.exe directly in bin/
                    var binDir = Path.Combine(gitDir, "bin");
                    if (File.Exists(Path.Combine(binDir, "git.exe")))
                    {
                        PathHelper.AddToPath(binDir, persist: true);
                        session.Log($"Portable Git {GitVersion} installed to {gitDir}.");
                    }
                    else
                    {
                        session.Log("Portable Git extracted but git.exe not found.");
                    }
                }
            }
            catch (Exception ex)
            {
                session.Log($"Failed to install portable Git: {ex.Message}");
            }
            finally
            {
                try { if (File.Exists(tempExe)) File.Delete(tempExe); }
                catch { /* best effort */ }
            }
        }

        /// <summary>
        /// Ensure Python is installed. Uses NuGet zip extraction (no MSI mutex conflict).
        /// </summary>
        private static void EnsurePython(Session session)
        {
            PathHelper.RefreshPath();
            if (PathHelper.CommandExists("python"))
            {
                session.Log("Python already installed.");
                ProcessRunner.UpdateStatus(session, "Python already installed, skipping...");
                return;
            }

            ProcessRunner.UpdateStatus(session, $"Installing Python {PythonVersion}...");

            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            var pythonDir = Path.Combine(localAppData, "python");
            var tempZip = Path.Combine(Path.GetTempPath(),
                $"python-{PythonVersion}.nupkg.zip");
            var extractDir = Path.Combine(Path.GetTempPath(), "python-nupkg");

            try
            {
                // Download Python NuGet package
                ProcessRunner.UpdateStatus(session,
                    $"Downloading Python {PythonVersion}...");
                DownloadFile(session, PythonNuGetUrl, tempZip);

                // Extract
                ProcessRunner.UpdateStatus(session, "Extracting Python...");
                if (Directory.Exists(extractDir))
                    Directory.Delete(extractDir, true);
                ZipFile.ExtractToDirectory(tempZip, extractDir);

                // NuGet python package has the full CPython in tools/
                var toolsDir = Path.Combine(extractDir, "tools");
                if (!Directory.Exists(toolsDir) ||
                    !File.Exists(Path.Combine(toolsDir, "python.exe")))
                {
                    session.Log("Python tools/ directory not found in NuGet package.");
                    return;
                }

                // Move to final location
                if (Directory.Exists(pythonDir))
                    Directory.Delete(pythonDir, true);
                Directory.Move(toolsDir, pythonDir);

                // Add to PATH (prepend to beat the WindowsApps Store stub)
                var scriptsDir = Path.Combine(pythonDir, "Scripts");
                PathHelper.AddToPath(pythonDir, persist: true);
                PathHelper.AddToPath(scriptsDir, persist: true);

                // Bootstrap pip
                ProcessRunner.UpdateStatus(session, "Bootstrapping pip...");
                var pythonExe = Path.Combine(pythonDir, "python.exe");
                ProcessRunner.Run(session, pythonExe,
                    "-m ensurepip --upgrade");

                session.Log($"Python {PythonVersion} installed to {pythonDir}.");
            }
            catch (Exception ex)
            {
                session.Log($"Failed to install Python: {ex.Message}");
            }
            finally
            {
                CleanupTemp(tempZip, extractDir);
            }
        }

        /// <summary>
        /// Ensure Azure CLI is installed. Uses pip install (no MSI mutex conflict).
        /// This is the slowest step - 3-5 minutes. Status text updates per package.
        /// </summary>
        private static void EnsureAzureCli(Session session)
        {
            PathHelper.RefreshPath();
            if (PathHelper.CommandExists("az"))
            {
                session.Log("Azure CLI already installed.");
                ProcessRunner.UpdateStatus(session,
                    "Azure CLI already installed, skipping...");
                return;
            }

            ProcessRunner.UpdateStatus(session,
                "Installing Azure CLI... this typically takes 5-7 minutes");

            var pythonExe = PathHelper.FindPython();
            if (pythonExe == null)
            {
                session.Log("Python not found. Cannot install Azure CLI.");
                return;
            }

            // pip install azure-cli (no live status - pip goes silent during
            // the install phase so text updates would just freeze)
            int exitCode = ProcessRunner.Run(session, pythonExe,
                "-m pip install azure-cli");

            PathHelper.RefreshPath();

            if (PathHelper.CommandExists("az"))
            {
                session.Log("Azure CLI installed successfully.");
            }
            else
            {
                // pip puts az.cmd in Python's Scripts dir - ensure it's on PATH
                var pythonDir = Path.GetDirectoryName(pythonExe);
                var scriptsDir = Path.Combine(pythonDir, "Scripts");
                if (File.Exists(Path.Combine(scriptsDir, "az.cmd")))
                {
                    PathHelper.AddToPath(scriptsDir);
                    session.Log($"Azure CLI installed (added {scriptsDir} to PATH).");
                }
                else
                {
                    session.Log("Azure CLI pip install completed but 'az' not found.");
                }
            }
        }

        /// <summary>
        /// Wrapper around EnsureAzureCli with synthetic progress.
        /// Drips ticks smoothly over 7 minutes while pip runs.
        /// </summary>
        private static void EnsureAzureCliWithProgress(Session session, int weight)
        {
            PathHelper.RefreshPath();
            if (PathHelper.CommandExists("az"))
            {
                session.Log("Azure CLI already installed.");
                ProcessRunner.UpdateStatus(session,
                    "Azure CLI already installed, skipping...");
                AdvanceProgress(session, weight);
                return;
            }

            RunWithSyntheticProgress(session,
                () => EnsureAzureCli(session),
                weight, targetSeconds: 420, reserveTicks: 5);
        }

        /// <summary>
        /// Ensure Node.js is installed. Uses zip extraction (no MSI mutex conflict).
        /// </summary>
        private static void EnsureNodeJs(Session session)
        {
            PathHelper.RefreshPath();
            if (PathHelper.CommandExists("node"))
            {
                session.Log("Node.js already installed.");
                ProcessRunner.UpdateStatus(session,
                    "Node.js already installed, skipping...");
                return;
            }

            ProcessRunner.UpdateStatus(session, $"Installing Node.js {NodeVersion}...");

            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            var nodeDir = Path.Combine(localAppData, "nodejs");
            var tempZip = Path.Combine(Path.GetTempPath(),
                $"node-{NodeVersion}-win-x64.zip");
            var extractDir = Path.Combine(Path.GetTempPath(), "nodejs-extract");

            try
            {
                ProcessRunner.UpdateStatus(session,
                    $"Downloading Node.js {NodeVersion}...");
                DownloadFile(session, NodeZipUrl, tempZip);

                ProcessRunner.UpdateStatus(session, "Extracting Node.js...");
                if (Directory.Exists(extractDir))
                    Directory.Delete(extractDir, true);
                ZipFile.ExtractToDirectory(tempZip, extractDir);

                // Node.js zip has an inner directory like "node-v22.14.0-win-x64"
                var innerDirs = Directory.GetDirectories(extractDir);
                if (innerDirs.Length == 0)
                {
                    session.Log("Node.js zip extraction produced no directories.");
                    return;
                }

                if (Directory.Exists(nodeDir))
                    Directory.Delete(nodeDir, true);
                Directory.Move(innerDirs[0], nodeDir);

                PathHelper.AddToPath(nodeDir, persist: true);
                session.Log($"Node.js {NodeVersion} installed to {nodeDir}.");
            }
            catch (Exception ex)
            {
                session.Log($"Failed to install Node.js: {ex.Message}");
            }
            finally
            {
                CleanupTemp(tempZip, extractDir);
            }
        }

        // =====================================================================
        // App setup steps
        // =====================================================================

        /// <summary>
        /// Bootstrap only a new checkout. Existing installs use the coordinator.
        /// Disables Git Credential Manager prompts (public repo).
        /// </summary>
        private static void CloneOrUpdateRepo(Session session, string installDir, string repoRef)
        {
            // Disable GCM popups
            Environment.SetEnvironmentVariable("GIT_TERMINAL_PROMPT", "0");
            Environment.SetEnvironmentVariable("GCM_INTERACTIVE", "never");

            string gitDir = Path.Combine(installDir, ".git");
            if (HasExistingInstallation(installDir))
                throw new InvalidOperationException(
                    "Existing installation requires the guarded upgrade coordinator.");

            if (Directory.Exists(installDir) && !Directory.Exists(gitDir))
            {
                // MSI already created this new directory for icon.ico.
                ProcessRunner.UpdateStatus(session,
                    "Initializing Sales Buddy repository...");
                session.Log("Directory exists, initializing git repo in-place.");
                RequireSuccess(session, "git", "init", installDir);
                RequireSuccess(session, "git", $"remote add origin {RepoUrl}", installDir);
            }

            if (!Directory.Exists(installDir))
            {
                // Fresh clone
                ProcessRunner.UpdateStatus(session,
                    "Cloning Sales Buddy repository...");
                PathHelper.RefreshPath();
                int exitCode = ProcessRunner.Run(session, "git",
                    $"-c credential.helper= clone {RepoUrl} \"{installDir}\"");
                if (exitCode != 0)
                {
                    throw new InvalidOperationException(
                        $"git clone failed with exit code {exitCode}");
                }
            }

            CheckoutRef(session, installDir, repoRef);
            session.Log("Repository ready.");
        }

        /// <summary>
        /// Point the working tree at the requested git ref (default "main").
        /// Fetches first so the ref is present even on a just-cloned repo, then
        /// checks it out onto the new local "main" branch used for builds.
        /// </summary>
        private static void CheckoutRef(Session session, string installDir, string repoRef)
        {
            RequireSuccess(session, "git", "-c credential.helper= fetch origin", installDir);
            RequireSuccess(session, "git", $"checkout -B main origin/{repoRef}", installDir);
        }

        /// <summary>
        /// Aggressively delete a directory: kill git processes, clear read-only
        /// flags, retry with delays.
        /// </summary>
        private static void ForceDeleteDirectory(Session session, string path)
        {
            // Kill any git processes that might hold locks
            foreach (var proc in Process.GetProcessesByName("git"))
            {
                try
                {
                    proc.Kill();
                    proc.WaitForExit(3000);
                    session.Log($"Killed git process {proc.Id}.");
                }
                catch { }
            }

            // Clear read-only attributes on all files (git pack files are read-only)
            try
            {
                foreach (string file in Directory.GetFiles(path, "*", SearchOption.AllDirectories))
                {
                    File.SetAttributes(file, System.IO.FileAttributes.Normal);
                }
            }
            catch (Exception ex)
            {
                session.Log($"Could not clear attributes: {ex.Message}");
            }

            // Retry delete with delays
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                try
                {
                    Directory.Delete(path, true);
                    session.Log($"Deleted {path} on attempt {attempt}.");
                    return;
                }
                catch (Exception ex)
                {
                    session.Log($"Force delete attempt {attempt}/3: {ex.Message}");
                    if (attempt < 3)
                        Thread.Sleep(2000);
                }
            }
            session.Log($"WARNING: Could not force-delete {path}.");
        }

        /// <summary>The production DB location: a sibling of the install dir under
        /// %LOCALAPPDATA%\SalesBuddy-data that survives any install-dir deletion.</summary>
        private static string ExternalDbPath()
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrEmpty(local)) return null;
            return Path.Combine(local, "SalesBuddy-data", "salesbuddy.db");
        }

        /// <summary>
        /// Resolve where the live database is (or should be). In production the DB
        /// lives OUTSIDE the install dir so no install / upgrade / uninstall can
        /// delete it. During the transitional upgrade that introduces the move the
        /// DB is still at the legacy in-install location until the app migrates it
        /// on first boot, so we prefer, in order:
        ///   1. the path the app published in installDir\data-path.txt,
        ///   2. the external location if a DB already exists there,
        ///   3. the legacy installDir\data location if a DB exists there,
        ///   4. otherwise the external location (the go-forward default).
        /// </summary>
        private static string ResolveDbPath(string installDir)
        {
            try
            {
                string pathFile = Path.Combine(installDir, "data-path.txt");
                if (File.Exists(pathFile))
                {
                    string published = File.ReadAllText(pathFile).Trim();
                    if (!string.IsNullOrEmpty(published)) return published;
                }
            }
            catch { }

            string external = ExternalDbPath();
            string legacy = Path.Combine(installDir, "data", "salesbuddy.db");
            try
            {
                if (!string.IsNullOrEmpty(external) && File.Exists(external)) return external;
                if (File.Exists(legacy)) return legacy;
            }
            catch { }
            return !string.IsNullOrEmpty(external) ? external : legacy;
        }

        /// <summary>
        /// Create a Python virtual environment and install dependencies.
        /// </summary>
        private static void SetupPythonEnv(Session session, string installDir)
        {
            var pythonExe = PathHelper.FindPython();
            if (pythonExe == null)
                throw new InvalidOperationException("Python not found.");

            var venvDir = Path.Combine(installDir, "venv");
            var venvPython = Path.Combine(venvDir, "Scripts", "python.exe");
            var pipExe = Path.Combine(venvDir, "Scripts", "pip.exe");
            var reqFile = Path.Combine(installDir, "requirements.txt");

            // Create venv if missing or broken (stale venv from failed uninstall)
            bool venvHealthy = File.Exists(venvPython) && File.Exists(pipExe);
            if (!venvHealthy)
            {
                // Remove stale venv remnants before recreating
                if (Directory.Exists(venvDir))
                {
                    session.Log("Removing stale venv...");
                    try { Directory.Delete(venvDir, true); }
                    catch (Exception ex)
                    {
                        session.Log($"WARNING: Could not remove stale venv: {ex.Message}");
                    }
                }

                ProcessRunner.UpdateStatus(session,
                    "Creating Python virtual environment...");
                RequireSuccess(session, pythonExe, $"-m venv \"{venvDir}\"", installDir);
                if (!File.Exists(venvPython))
                    throw new InvalidOperationException("Failed to create venv.");
                session.Log("Virtual environment created.");
            }
            else
            {
                session.Log("Virtual environment already exists and is healthy.");
            }

            // Always run pip install (handles fresh install and repair)
            if (File.Exists(reqFile))
            {
                ProcessRunner.UpdateStatus(session,
                    "Installing Python dependencies... this typically takes 2-3 minutes");
                RequireSuccess(session, pipExe, $"install -r \"{reqFile}\"", installDir);
                session.Log("Dependencies installed.");

                // Verify the venv is actually usable. A partial/interrupted pip
                // leaves a dependency-less venv ("No module named flask") that
                // crash-loops the backend and, under the electron shell, shows a
                // dead localhost in the browser (observed 2026-07-23). Probe +
                // one retry so a transient pip hiccup can't ship a broken venv.
                if (!VenvImportsFlask(session, venvPython))
                {
                    session.Log("Backend deps missing after install - retrying pip once.");
                    ProcessRunner.UpdateStatus(session,
                        "Repairing Python dependencies...");
                    RequireSuccess(session, venvPython,
                        $"-m pip install -r \"{reqFile}\"", installDir);
                    if (!VenvImportsFlask(session, venvPython))
                        throw new InvalidOperationException("Backend dependencies failed verification.");
                }
                else
                {
                    session.Log("Verified: backend deps import OK.");
                }
            }
            else
            {
                throw new InvalidOperationException($"requirements.txt not found at {reqFile}");
            }
        }

        /// <summary>
        /// Probe whether the installed venv can import flask (i.e. deps are
        /// actually present). Never throws.
        /// </summary>
        private static bool VenvImportsFlask(Session session, string venvPython)
        {
            try
            {
                return ProcessRunner.Run(session, venvPython, "-c \"import flask\"") == 0;
            }
            catch (Exception ex)
            {
                session.Log($"venv flask probe error: {ex.Message}");
                return false;
            }
        }

        /// <summary>
        /// Create first-install configuration. The supervisor initializes the database.
        /// </summary>
        private static void ConfigureApp(Session session, string installDir)
        {
            var envFile = Path.Combine(installDir, ".env");
            var exampleFile = Path.Combine(installDir, ".env.example");

            // Create .env from example if it doesn't exist
            if (!File.Exists(envFile) && File.Exists(exampleFile))
            {
                ProcessRunner.UpdateStatus(session, "Creating configuration file...");
                string content = File.ReadAllText(exampleFile);

                // Generate a random secret key
                string secretKey = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
                content = content.Replace(
                    "your-secret-key-here-change-in-production", secretKey);

                File.WriteAllText(envFile, content);
                session.Log(".env created.");
            }
        }

        /// <summary>
        /// Create Start Menu and/or desktop shortcuts.
        /// </summary>
        private static void CreateShortcuts(Session session, string installDir,
            bool startMenu, bool desktop)
        {
            int port = GetPortFromEnv(installDir);
            string appUrl = $"http://localhost:{port}";
            string explorer = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                "explorer.exe");

            // Find icon - prefer MSI-installed copy, fall back to repo
            string iconPath = Path.Combine(installDir, "icon.ico");
            if (!File.Exists(iconPath))
                iconPath = Path.Combine(installDir, "static", "icon.ico");

            if (startMenu)
            {
                var startMenuFolder = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "Microsoft", "Windows", "Start Menu", "Programs", "Sales Buddy");

                if (!Directory.Exists(startMenuFolder))
                    Directory.CreateDirectory(startMenuFolder);

                // Main app shortcut
                CreateShortcutLink(
                    Path.Combine(startMenuFolder, $"{AppName} (web).lnk"),
                    explorer, appUrl, "",
                    File.Exists(iconPath) ? $"{iconPath},0" : "",
                    "Open Sales Buddy in your browser");

                // Start Server
                CreateShortcutLink(
                    Path.Combine(startMenuFolder, "Start Server.lnk"),
                    Path.Combine(installDir, "start.bat"), "",
                    installDir,
                    File.Exists(iconPath) ? $"{iconPath},0" : "",
                    "Start the Sales Buddy server");

                // Stop Server
                CreateShortcutLink(
                    Path.Combine(startMenuFolder, "Stop Server.lnk"),
                    Path.Combine(installDir, "stop.bat"), "",
                    installDir, "",
                    "Stop the Sales Buddy server");

                // Update
                CreateShortcutLink(
                    Path.Combine(startMenuFolder, "Update.lnk"),
                    Path.Combine(installDir, "update.bat"), "",
                    installDir,
                    File.Exists(iconPath) ? $"{iconPath},0" : "",
                    "Update Sales Buddy to the latest version");

                session.Log("Start Menu shortcuts created.");
            }

            if (desktop)
            {
                var desktopPath = Environment.GetFolderPath(
                    Environment.SpecialFolder.DesktopDirectory);
                CreateShortcutLink(
                    Path.Combine(desktopPath, $"{AppName} (web).lnk"),
                    explorer, appUrl, "",
                    File.Exists(iconPath) ? $"{iconPath},0" : "",
                    "Open Sales Buddy in your browser");
                session.Log("Desktop shortcut created.");
            }
        }

        /// <summary>
        /// Register a Windows Task Scheduler task to start the server on login.
        /// </summary>
        /// <summary>
        /// Build and wire the Electron desktop shell by running the shared
        /// scripts/migrate-to-electron.ps1. Passes -SkipPull (the MSI already
        /// updated the repo) and -NoLaunch (the Exit dialog launches the app),
        /// and honors the shortcut/auto-start options. Returns true only if the
        /// packaged exe exists afterward, so callers can fall back to web wiring.
        /// </summary>
        private static bool RunElectronSetup(Session session, string installDir,
            bool startMenu, bool desktop, bool autoStart)
        {
            string script = Path.Combine(installDir, "scripts", "migrate-to-electron.ps1");
            if (!File.Exists(script))
            {
                session.Log($"migrate-to-electron.ps1 not found at {script}; skipping Electron setup.");
                return false;
            }

            string baseArgs = $"-ExecutionPolicy Bypass -NoProfile -File \"{script}\" -SkipPull -NoLaunch";
            string opts = "";
            if (!desktop) opts += " -NoDesktop";
            if (!startMenu) opts += " -NoStartMenu";
            if (!autoStart) opts += " -NoAutoStart";

            // First pass: full rebuild of the shell from source.
            int exitCode = ProcessRunner.Run(session, "powershell.exe", baseArgs + " -Rebuild" + opts,
                workingDirectory: installDir);

            string exe = Path.Combine(installDir, "electron-dist", "Sales Buddy.exe");
            bool ok = exitCode == 0 && File.Exists(exe);
            session.Log($"Electron setup {(ok ? "succeeded" : "did NOT produce the exe")}: {exe}");
            return ok;
        }

        /// <summary>
        /// True if the Electron shell (unique product process name "Sales Buddy")
        /// is running. Never throws - a detection hiccup reports "not running".
        /// </summary>
        private static bool IsShellRunning(Session session)
        {
            try
            {
                foreach (var p in Process.GetProcessesByName("Sales Buddy"))
                {
                    p.Dispose();
                    return true;
                }
            }
            catch (Exception ex)
            {
                session.Log($"IsShellRunning: detection error (treating as not running): {ex.Message}");
            }
            return false;
        }

        /// <summary>
        /// Ask the user (interactive installs only) for permission to close the
        /// running app before we do it. OK -> proceed to close; Cancel -> caller
        /// aborts the install. Shown through the MSI UI so it rides on the install
        /// dialog rather than a detached window.
        /// </summary>
        private static MessageResult PromptCloseApp(Session session)
        {
            using (Record rec = new Record(0))
            {
                rec.FormatString =
                    "Sales Buddy is currently open and needs to close to finish updating.\n\n" +
                    "Click OK to close it now and continue, or Cancel to stop and close it yourself.";
                return session.Message(
                    InstallMessage.User
                        | (InstallMessage)MessageButtons.OKCancel
                        | (InstallMessage)MessageIcon.Warning,
                    rec);
            }
        }

        /// <summary>
        /// Close a running Sales Buddy shell so the install can proceed without the
        /// user manually quitting from the tray. Two stages: (1) drop the
        /// shutdown.request sentinel the shell watches and give it a few seconds to
        /// quit itself cleanly (removes the tray icon, releases the single-instance
        /// lock, tears down its own backend); (2) if it's still up, force-kill the
        /// shell + this install's backend via StopRunningApp, which works for ANY
        /// prior shell version. The installer's DB snapshot/restore covers any WAL a
        /// non-clean shutdown might leave.
        /// </summary>
        private static void CloseRunningApp(Session session, string installDir)
        {
            // 1. Graceful: drop the sentinel a sentinel-aware shell (1.1.15+) watches.
            try
            {
                string dataDir = Path.Combine(installDir, "data");
                Directory.CreateDirectory(dataDir);
                string sentinel = Path.Combine(dataDir, "shutdown.request");
                File.WriteAllText(sentinel, DateTime.UtcNow.ToString("o"));
                session.Log($"CloseRunningApp: dropped shutdown sentinel at {sentinel}");
            }
            catch (Exception ex)
            {
                session.Log($"CloseRunningApp: could not drop sentinel (will force-close): {ex.Message}");
            }

            // 2. Give a sentinel-aware shell up to ~8s to exit on its own.
            for (int i = 0; i < 16 && IsShellRunning(session); i++)
            {
                Thread.Sleep(500);
            }

            if (IsShellRunning(session))
            {
                // 3. Fallback: force-close shell + backend for THIS install (any version).
                session.Log("CloseRunningApp: shell still up after grace period - force-closing.");
                StopRunningApp(session, installDir);
            }
            else
            {
                session.Log("CloseRunningApp: shell exited on its own.");
                try { StopBackendProcesses(session, installDir); } catch { }
            }

            // ALWAYS remove the sentinel now that the app is down - whether it quit
            // on its own or we force-killed it. A leftover request would be read by
            // the freshly-installed shell we launch later, making it quit ~10s after
            // boot (observed 2026-07-24). The shell also clears a stale sentinel on
            // boot as belt-and-suspenders.
            try
            {
                string sentinel = Path.Combine(installDir, "data", "shutdown.request");
                if (File.Exists(sentinel)) File.Delete(sentinel);
            }
            catch (Exception ex)
            {
                session.Log($"CloseRunningApp: sentinel cleanup failed: {ex.Message}");
            }
        }

        /// <summary>
        /// Stop any running Sales Buddy (shell + backend) for THIS install before
        /// the installer touches the repo, venv, or shell. A running app holds file
        /// handles that make git clean, pip install, and the shell restage race and
        /// leave a corrupt install - observed 2026-07-17 as a half-staged
        /// electron-dist (missing icudtl.dat -> ICU crash) plus a dependency-less
        /// venv. No-op on a fresh box (nothing matches).
        /// </summary>
        private static void StopRunningApp(Session session, string installDir)
        {
            string escapedRoot = installDir.TrimEnd('\\').Replace("'", "''") + "\\";
            string script = @"
$ErrorActionPreference = 'Stop'
$root = '" + escapedRoot + @"'
function Get-InstallProcesses {
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $owned = @{}
    foreach ($item in $all) {
        if ($item.ProcessId -eq $PID) { continue }
        $path = [string]$item.ExecutablePath
        $command = [string]$item.CommandLine
        $appProcess = $item.Name -in @('Sales Buddy.exe', 'python.exe', 'pythonw.exe',
                                      'waitress-serve.exe')
        $launcher = $item.Name -in @('powershell.exe', 'pwsh.exe', 'wscript.exe',
                                    'cscript.exe') -and
                    $command -match '(server\.ps1|run-hidden\.vbs|startup\.ps1)'
        if ($path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or
            (($appProcess -or $launcher) -and
             $command.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
            $owned[[int]$item.ProcessId] = $item
        }
        elseif ($item.Name -in @('Sales Buddy.exe', 'python.exe', 'pythonw.exe',
                                'waitress-serve.exe') -and !$path -and !$command) {
            throw 'Cannot establish process ownership. Close Sales Buddy and retry.'
        }
    }
    do {
        $added = $false
        foreach ($item in $all) {
            if ($item.ProcessId -ne $PID -and
                $owned.ContainsKey([int]$item.ParentProcessId) -and
                !$owned.ContainsKey([int]$item.ProcessId)) {
                $owned[[int]$item.ProcessId] = $item
                $added = $true
            }
        }
    } while ($added)
    @($owned.Values)
}
for ($attempt = 0; $attempt -lt 6; $attempt++) {
    $owned = @(Get-InstallProcesses)
    if (!$owned.Count) { exit 0 }
    # Stop parents first so supervisors cannot restart their children.
    foreach ($item in ($owned | Sort-Object CreationDate)) {
        $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $item.ProcessId -Force -ErrorAction Stop
            if (!$process.WaitForExit(5000)) { throw 'An install process did not exit.' }
        }
    }
    Start-Sleep -Milliseconds 500
}
if (@(Get-InstallProcesses).Count) { throw 'Sales Buddy processes are still running.' }
";
            int exitCode = ProcessRunner.RunPowerShell(session, script);
            if (exitCode != 0)
                throw new InvalidOperationException(
                    "Cannot prove Sales Buddy has stopped. No upgrade was applied. " +
                    "Close the desktop app and its backend, then retry.");

            string exe = Path.Combine(installDir, "electron-dist", "Sales Buddy.exe");
            for (int i = 0; i < 20 && File.Exists(exe); i++)
            {
                try
                {
                    using (File.Open(exe, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { break; }
                }
                catch { Thread.Sleep(500); }
            }
            if (File.Exists(exe))
            {
                // Failure to release the shell is fatal, never a best-effort warning.
                using (File.Open(exe, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
            }
            string sentinel = Path.Combine(installDir, "data", "shutdown.request");
            if (File.Exists(sentinel)) File.Delete(sentinel);
            session.Log("StopRunningApp: complete.");
        }

        private static Process[] SafeGetProcesses(string name)
        {
            try { return Process.GetProcessesByName(name); }
            catch { return new Process[0]; }
        }

        /// <summary>
        /// Kill stray backend processes (waitress / python worker) launched from
        /// THIS install's venv, scoped by path so it never touches other Pythons.
        /// Used by CheckAppRunning to clear orphans a buggy pre-1.1.9 tray Quit
        /// left behind, before the upgrade's uninstall backs up and deletes the DB.
        /// </summary>
        private static void StopBackendProcesses(Session session, string installDir)
        {
            string installLower = installDir.TrimEnd('\\').ToLowerInvariant();
            foreach (var name in new[] { "python", "pythonw", "waitress-serve" })
            {
                foreach (var p in SafeGetProcesses(name))
                {
                    string path = "";
                    try { path = (p.MainModule?.FileName ?? "").ToLowerInvariant(); }
                    catch { /* cross-bitness / access denied - skip */ }
                    if (path.Length > 0 && path.StartsWith(installLower))
                    {
                        TryKill(session, p);
                    }
                }
            }
        }

        private static void TryKill(Session session, Process p)
        {
            try
            {
                p.Kill();
                p.WaitForExit(5000);
                session.Log($"StopRunningApp: killed {p.ProcessName} pid {p.Id}.");
            }
            catch (Exception ex)
            {
                session.Log($"StopRunningApp: could not kill pid {p.Id}: {ex.Message}");
            }
        }

        private static void ConfigureAutoStartTask(Session session, string installDir)
        {
            string serverScript = Path.Combine(installDir, "scripts", "server.ps1");
            string vbsLauncher = Path.Combine(installDir, "scripts", "run-hidden.vbs");
            if (!File.Exists(serverScript) || !File.Exists(vbsLauncher))
            {
                session.Log("server.ps1 or run-hidden.vbs not found, skipping auto-start configuration.");
                return;
            }

            // Use wscript + VBS launcher so the autostart never flashes a console
            // window at login. powershell.exe -WindowStyle Hidden still flashes briefly;
            // wscript starts the process with no console at all.
            RequireSuccess(session, "schtasks.exe",
                $"/create /tn \"SalesBuddy-AutoStart\" " +
                $"/tr \"wscript.exe \\\"{vbsLauncher}\\\" \\\"{serverScript}\\\"\" " +
                $"/sc ONLOGON /rl LIMITED /f", installDir);
            session.Log("Auto-start task created.");
        }

        /// <summary>
        /// Register the SalesBuddy-DailyBackup scheduled task that runs
        /// backup.ps1 -Silent once a day. Idempotent; /f overwrites if it
        /// already exists. The backup script no-ops gracefully when no
        /// OneDrive path is configured, so it is safe to register the task
        /// even if the user hasn't set up backups yet.
        /// </summary>
        private static void ConfigureBackupTask(Session session, string installDir)
        {
            string backupScript = Path.Combine(installDir, "scripts", "backup.ps1");
            string vbsLauncher = Path.Combine(installDir, "scripts", "run-hidden.vbs");
            if (!File.Exists(backupScript) || !File.Exists(vbsLauncher))
            {
                session.Log("backup.ps1 or run-hidden.vbs not found, skipping daily backup task.");
                return;
            }

            // wscript + VBS launcher avoids the brief console flash that
            // powershell.exe -WindowStyle Hidden still produces.
            // Daily at 11:00 AM matches what scripts/backup.ps1 -Setup
            // and scripts/server.ps1 register.
            RequireSuccess(session, "schtasks.exe",
                $"/create /tn \"SalesBuddy-DailyBackup\" " +
                $"/tr \"wscript.exe \\\"{vbsLauncher}\\\" \\\"{backupScript}\\\" -Silent\" " +
                $"/sc DAILY /st 11:00 /rl LIMITED /f", installDir);
            session.Log("Daily backup task created.");
        }

        /// <summary>
        /// Start the Sales Buddy supervisor (which spawns the web server and the
        /// background worker) in the background.
        /// </summary>
        private static void StartServer(Session session, string installDir, string bootId = null)
        {
            int port = GetPortFromEnv(installDir);
            var python = Path.Combine(installDir, "venv", "Scripts",
                "python.exe");

            if (!File.Exists(python))
            {
                throw new InvalidOperationException("python.exe not found. Server not started.");
            }

            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-m app.supervisor",
                WorkingDirectory = installDir,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            // The supervisor reads the port from the environment and passes it
            // through to the web child it spawns.
            psi.EnvironmentVariables["PORT"] = port.ToString();
            if (bootId != null) psi.EnvironmentVariables["SALESBUDDY_BOOT_ID"] = bootId;

            if (Process.Start(psi) == null)
                throw new InvalidOperationException("The supervisor did not start.");
            session.Log($"Supervisor (web + worker) started on port {port}.");
        }

        // =====================================================================
        // Uninstall helpers
        // =====================================================================

        /// <summary>
        /// Stop the Sales Buddy server by killing waitress and python processes
        /// running from the install directory, then waiting for handles to release.
        /// </summary>
        private static void StopServer(Session session, string installDir)
        {
            var killed = new List<Process>();

            // Kill waitress-serve processes
            foreach (var proc in Process.GetProcessesByName("waitress-serve"))
            {
                try
                {
                    proc.Kill();
                    killed.Add(proc);
                    session.Log($"Killed waitress-serve process {proc.Id}.");
                }
                catch (Exception ex)
                {
                    session.Log($"Could not kill waitress-serve {proc.Id}: {ex.Message}");
                }
            }

            // Kill python processes running from our venv
            string venvDir = Path.Combine(installDir, "venv").TrimEnd('\\');
            foreach (var proc in Process.GetProcessesByName("python"))
            {
                try
                {
                    string exePath = proc.MainModule?.FileName ?? "";
                    if (exePath.StartsWith(venvDir, StringComparison.OrdinalIgnoreCase))
                    {
                        proc.Kill();
                        killed.Add(proc);
                        session.Log($"Killed python process {proc.Id} ({exePath}).");
                    }
                }
                catch (Exception ex)
                {
                    session.Log($"Could not inspect/kill python {proc.Id}: {ex.Message}");
                }
            }

            // Wait for all killed processes to fully exit and release file handles
            foreach (var proc in killed)
            {
                try
                {
                    proc.WaitForExit(5000);
                }
                catch { /* already dead */ }
            }

            // Extra settle time for OS to release file locks
            if (killed.Count > 0)
            {
                Thread.Sleep(1000);
            }
        }

        /// <summary>
        /// Remove Sales Buddy scheduled tasks.
        /// </summary>
        private static void RemoveScheduledTasks(Session session)
        {
            var taskNames = new[] { "SalesBuddy-AutoStart", "SalesBuddy-DailyBackup" };
            foreach (var taskName in taskNames)
            {
                ProcessRunner.Run(session, "schtasks.exe",
                    $"/delete /tn \"{taskName}\" /f");
            }

            // Electron-era installs autostart via an HKCU Run entry rather than a
            // scheduled task (the migration runs non-elevated and can't manage a
            // root task). Clean that up too so uninstall leaves nothing behind.
            try
            {
                using (var runKey = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Run", writable: true))
                {
                    if (runKey?.GetValue("SalesBuddy") != null)
                    {
                        runKey.DeleteValue("SalesBuddy", throwOnMissingValue: false);
                        session.Log("Removed HKCU Run autostart entry.");
                    }
                }
            }
            catch (Exception ex)
            {
                session.Log($"Could not remove HKCU Run entry: {ex.Message}");
            }
        }

        /// <summary>
        /// Remove Start Menu and desktop shortcuts.
        /// </summary>
        private static void RemoveShortcuts(Session session)
        {
            var startMenuFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "Microsoft", "Windows", "Start Menu", "Programs", "Sales Buddy");

            if (Directory.Exists(startMenuFolder))
            {
                Directory.Delete(startMenuFolder, true);
                session.Log("Start Menu shortcuts removed.");
            }

            var desktopShortcut = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                $"{AppName} (web).lnk");

            if (File.Exists(desktopShortcut))
            {
                File.Delete(desktopShortcut);
                session.Log("Desktop shortcut removed.");
            }

            // Also clean up old-style shortcut name
            var oldShortcut = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                $"{AppName}.lnk");
            if (File.Exists(oldShortcut))
                File.Delete(oldShortcut);
        }

        // =====================================================================
        // Utilities
        // =====================================================================

        /// <summary>
        /// Install a package via winget with retry logic for MSI mutex conflicts.
        /// </summary>
        private static void InstallViaWinget(Session session, string name,
            string packageId, string testCommand)
        {
            const int maxAttempts = 3;
            for (int attempt = 1; attempt <= maxAttempts; attempt++)
            {
                int exitCode = ProcessRunner.Run(session, "winget",
                    $"install {packageId} --silent " +
                    "--accept-package-agreements --accept-source-agreements");

                if (exitCode == 0)
                {
                    PathHelper.RefreshPath();
                    session.Log($"{name} installed successfully.");
                    return;
                }

                // Exit code -1978335189 means "already installed"
                if (exitCode == -1978335189 || exitCode == unchecked((int)0x8A150019))
                {
                    PathHelper.RefreshPath();
                    session.Log($"{name} already installed (winget confirmed).");
                    return;
                }

                // Exit code -1978334974 wraps MSI error 1618 (mutex conflict).
                // Wait and retry. Use Thread.Sleep since we're in a deferred CA.
                if ((exitCode == -1978334974 || exitCode == unchecked((int)0x8A150022))
                    && attempt < maxAttempts)
                {
                    session.Log($"{name} install hit MSI mutex. " +
                        $"Waiting 15s before retry ({attempt}/{maxAttempts})...");
                    ProcessRunner.UpdateStatus(session,
                        $"Waiting for another installer to finish ({attempt}/{maxAttempts})...");
                    System.Threading.Thread.Sleep(15000);
                    continue;
                }

                session.Log($"Failed to install {name} (exit code: {exitCode}).");
                return;
            }
        }

        /// <summary>
        /// Download a file from a URL.
        /// </summary>
        private static void DownloadFile(Session session, string url, string destPath)
        {
            session.Log($"Downloading {url}");
            using (var client = new WebClient())
            {
                client.DownloadFile(url, destPath);
            }
            session.Log($"Downloaded to {destPath}");
        }

        /// <summary>
        /// Read the PORT setting from the app's .env file.
        /// </summary>
        private static int GetPortFromEnv(string installDir)
        {
            var envFile = Path.Combine(installDir, ".env");
            if (File.Exists(envFile))
            {
                foreach (var line in File.ReadAllLines(envFile))
                {
                    var match = Regex.Match(line, @"^\s*PORT\s*=\s*(\d+)");
                    if (match.Success)
                        return int.Parse(match.Groups[1].Value);
                }
            }
            return DefaultPort;
        }

        /// <summary>
        /// Read a boolean value from CustomActionData.
        /// Returns true if the key exists and its value is "1".
        /// </summary>
        private static bool GetBoolData(CustomActionData data, string key)
        {
            return data.ContainsKey(key) && !string.IsNullOrEmpty(data[key])
                && data[key] != "0";
        }

        /// <summary>
        /// Read the git ref the installer should build from (REPOREF property).
        /// Defaults to "main". Sanitized to ref-safe characters so an override
        /// can never inject extra arguments into the git commands.
        /// </summary>
        private static string GetRepoRef(CustomActionData data)
        {
            string reff = data.ContainsKey("REPOREF") ? (data["REPOREF"] ?? "").Trim() : "";
            if (reff.Length == 0) return "main";
            foreach (char c in reff)
            {
                bool ok = char.IsLetterOrDigit(c) || c == '/' || c == '.'
                    || c == '_' || c == '-';
                if (!ok) return "main";
            }
            return reff;
        }

        /// <summary>
        /// Prepend locally-installed tool directories to the process PATH
        /// so they take priority over Windows Store stubs.
        /// </summary>
        private static void PrependLocalTools()
        {
            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            var dirs = new[]
            {
                Path.Combine(localAppData, "python"),
                Path.Combine(localAppData, "python", "Scripts"),
                Path.Combine(localAppData, "nodejs"),
                Path.Combine(localAppData, "git", "cmd"),
            };
            foreach (var dir in dirs)
            {
                if (Directory.Exists(dir))
                    PathHelper.AddToPath(dir);
            }
        }

        /// <summary>
        /// Create a Windows shortcut (.lnk) using the WScript.Shell COM object.
        /// </summary>
        private static void CreateShortcutLink(string shortcutPath, string targetPath,
            string arguments, string workingDirectory, string iconLocation,
            string description)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            dynamic shell = Activator.CreateInstance(shellType);
            try
            {
                dynamic shortcut = shell.CreateShortcut(shortcutPath);
                try
                {
                    shortcut.TargetPath = targetPath;
                    if (!string.IsNullOrEmpty(arguments))
                        shortcut.Arguments = arguments;
                    if (!string.IsNullOrEmpty(workingDirectory))
                        shortcut.WorkingDirectory = workingDirectory;
                    if (!string.IsNullOrEmpty(iconLocation))
                        shortcut.IconLocation = iconLocation;
                    if (!string.IsNullOrEmpty(description))
                        shortcut.Description = description;
                    shortcut.Save();
                }
                finally
                {
                    Marshal.ReleaseComObject(shortcut);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(shell);
            }
        }

        /// <summary>
        /// Clean up temporary download and extraction files.
        /// </summary>
        private static void CleanupTemp(string zipPath, string extractDir)
        {
            try { if (File.Exists(zipPath)) File.Delete(zipPath); }
            catch { /* best effort */ }
            try { if (Directory.Exists(extractDir)) Directory.Delete(extractDir, true); }
            catch { /* best effort */ }
        }
    }
}
