using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using WixToolset.Dtf.WindowsInstaller;

namespace SalesBuddy.CustomActions
{
    public partial class InstallerActions
    {
        /// <summary>Recognize damaged installations too, rather than treating them as new.</summary>
        private static bool HasExistingInstallation(string root)
        {
            return Directory.Exists(Path.Combine(root, ".git"))
                || File.Exists(Path.Combine(root, ".git"))
                || Directory.Exists(Path.Combine(root, "app"))
                || Directory.Exists(Path.Combine(root, "venv"))
                || Directory.Exists(Path.Combine(root, "electron-dist"))
                || Directory.Exists(Path.Combine(root, "electron"))
                || Directory.Exists(Path.Combine(root, "scripts"))
                || Directory.Exists(Path.Combine(root, "data"))
                || File.Exists(Path.Combine(root, ".env"))
                || File.Exists(Path.Combine(root, "requirements.txt"))
                || File.Exists(Path.Combine(root, "start.bat"))
                || File.Exists(Path.Combine(root, "data-path.txt"));
        }

        /// <summary>Snapshot, apply, rebuild and accept through the shared offline coordinator.</summary>
        private static ActionResult UpgradeExistingInstallation(
            Session session, string root, string repoRef,
            bool startMenu, bool desktop, bool autoStart)
        {
            string python = Path.Combine(root, "venv", "Scripts", "python.exe");
            if (!File.Exists(python) || !File.Exists(Path.Combine(root, "app", "db_paths.py"))
                || (!Directory.Exists(Path.Combine(root, ".git"))
                    && !File.Exists(Path.Combine(root, ".git"))))
                throw new InvalidOperationException(
                    "Existing installation is missing its repository, runtime or shared database " +
                    "resolver. Nothing has been replaced. Restore a known-good installation or " +
                    "use explicit recovery; do not delete the installation or database.");

            PrependLocalTools();
            if (!PathHelper.FindGit(session) || !PathHelper.CommandExists("node"))
                throw new InvalidOperationException(
                    "Existing upgrades require working Git and Node.js. Repair prerequisites " +
                    "separately, then retry. Recovery does not undo system prerequisite changes.");
            RequireSuccess(session, python, "-c \"import dotenv\"", root);
            RequireSuccess(session, "git", "rev-parse --verify HEAD", root);
            StopRunningApp(session, root);

            using (var coordinator = new UpgradeCoordinator(root))
            {
                session.Log("Recovery protects application code, runtime, desktop files and the " +
                    "database only; it does not undo external Python or prerequisite changes.");
                ProcessRunner.UpdateStatus(session, "Saving a matched recovery point...");
                coordinator.Run(session, "prepare", $"--target origin/{repoRef} --shell-rebuild");
                string phase = "apply";
                try
                {
                    ProcessRunner.UpdateStatus(session, "Applying verified Sales Buddy upgrade...");
                    coordinator.Run(session, "apply");
                    phase = "shell";
                    ProcessRunner.UpdateStatus(session, "Building the desktop app...");
                    if (!RunElectronSetup(session, root, startMenu, desktop, autoStart))
                        throw new InvalidOperationException(
                            "Desktop build failed. The pending upgrade remains recoverable; " +
                            "no old-code or web fallback was started.");
                    ConfigureBackupTask(session, root);
                    phase = "startup";
                    ProcessRunner.UpdateStatus(session, "Verifying the new backend and worker...");
                    LaunchAndVerify(session, root, coordinator, desktopShell: true, accept: true);
                }
                catch
                {
                    // apply records its own precise failure phase and diagnostic.
                    if (phase != "apply")
                    {
                        try { coordinator.Run(session, "fail", "--phase " + phase); }
                        catch (Exception error)
                        {
                            session.Log("Could not record installer failure: " + error.Message);
                        }
                    }
                    StopRunningApp(session, root);
                    coordinator.Run(session, "launch-recovery");
                    ProcessRunner.UpdateStatus(session,
                        "Upgrade failed. Saved-version recovery has started. Diagnostics are " +
                        "submitted automatically; dismiss the notice when restoration finishes.");
                    throw;
                }
            }
            ProcessRunner.UpdateStatus(session, "Installation complete!");
            session.Log("=== Sales Buddy Upgrade Accepted After Fresh Backend/Worker Readiness ===");
            return ActionResult.Success;
        }

        /// <summary>Fail closed on every mandatory subprocess exit code.</summary>
        private static void RequireSuccess(
            Session session, string executable, string arguments, string root)
        {
            int code = ProcessRunner.Run(session, executable, arguments, workingDirectory: root);
            if (code != 0)
                throw new InvalidOperationException(
                    $"{Path.GetFileName(executable)} failed (exit {code}). " +
                    "See the installer log and Sales Buddy startup diagnostics. " +
                    "Failed upgrades automatically restore the saved version and queue diagnostics.");
        }

        /// <summary>Read a full commit without trusting an old startup-state file.</summary>
        private static string InstalledCommit(string root)
        {
            var info = new ProcessStartInfo("git", "rev-parse --verify HEAD")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using (var process = Process.Start(info))
            {
                process.BeginErrorReadLine();
                string commit = process.StandardOutput.ReadToEnd().Trim();
                process.WaitForExit();
                if (process.ExitCode != 0 || !Regex.IsMatch(commit, @"\A[0-9a-f]{40}\z"))
                    throw new InvalidOperationException("Cannot verify the installed full commit.");
                return commit;
            }
        }

        /// <summary>Ignore stale boot state and verify the exact newly launched revision.</summary>
        private static void LaunchAndVerify(
            Session session, string root, UpgradeCoordinator coordinator,
            bool desktopShell, bool accept)
        {
            string expected = InstalledCommit(root);
            string statePath = Path.Combine(root, "data", "startup-state.json");
            string previousBoot = null;
            if (File.Exists(statePath))
            {
                try
                {
                    if (new FileInfo(statePath).Length > 262144)
                        throw new InvalidOperationException("Startup status is oversized.");
                    var previous = new JavaScriptSerializer()
                        .Deserialize<Dictionary<string, object>>(File.ReadAllText(statePath));
                    object value;
                    if (previous != null && previous.TryGetValue("boot_id", out value))
                        previousBoot = value as string;
                }
                catch (ArgumentException) { }
            }
            // The old processes have been stopped. Removing their status prevents
            // an old ready document from qualifying as a newly launched supervisor.
            if (File.Exists(statePath)) File.Delete(statePath);
            DateTime launchedAt = DateTime.UtcNow;
            string requestedBoot = Guid.NewGuid().ToString("N");
            if (desktopShell)
            {
                string exe = Path.Combine(root, "electron-dist", "Sales Buddy.exe");
                if (!File.Exists(exe))
                    throw new InvalidOperationException("The rebuilt desktop shell is missing.");
                var info = new ProcessStartInfo(exe, "--startup --minimized")
                {
                    WorkingDirectory = root,
                    UseShellExecute = false,
                };
                info.EnvironmentVariables["SALESBUDDY_BOOT_ID"] = requestedBoot;
                if (Process.Start(info) == null)
                    throw new InvalidOperationException("The desktop shell did not start.");
            }
            else
            {
                StartServer(session, root, requestedBoot);
            }

            string boot = null;
            DateTime deadline = DateTime.UtcNow.AddSeconds(120);
            while (DateTime.UtcNow < deadline)
            {
                if (File.Exists(statePath)
                    && File.GetLastWriteTimeUtc(statePath) >= launchedAt.AddSeconds(-1))
                {
                    Dictionary<string, object> state = null;
                    try
                    {
                        var file = new FileInfo(statePath);
                        if (file.Length > 262144)
                            throw new InvalidOperationException("Startup status is oversized.");
                        state = new JavaScriptSerializer()
                            .Deserialize<Dictionary<string, object>>(File.ReadAllText(statePath));
                    }
                    catch (IOException) { }
                    catch (ArgumentException) { }
                    object value;
                    if (state != null && state.TryGetValue("expected_commit", out value)
                        && string.Equals(value as string, expected, StringComparison.Ordinal)
                        && state.TryGetValue("boot_id", out value))
                    {
                        Guid parsed;
                        string candidate = value as string;
                        if (candidate != null && candidate != previousBoot
                            && Guid.TryParse(candidate, out parsed))
                        {
                            boot = candidate;
                            break;
                        }
                    }
                }
                Thread.Sleep(500);
            }
            if (boot == null)
                throw new InvalidOperationException(
                    "No fresh target supervisor started. Open Sales Buddy for startup diagnostics.");
            string options = $"--boot-id {boot} --port {GetPortFromEnv(root)}";
            coordinator.Run(session, "wait-ready", options);
            if (accept) coordinator.Run(session, "accept", options);
        }

        /// <summary>
        /// Run the MSI's coordinator using the installed runtime even on the first
        /// safety rollout, when the old checkout does not yet contain upgrade.py.
        /// </summary>
        private sealed class UpgradeCoordinator : IDisposable
        {
            private readonly string root;
            private readonly string runner;

            public UpgradeCoordinator(string root)
            {
                this.root = root;
                string data = Path.Combine(root, "data");
                Directory.CreateDirectory(data);
                runner = Path.Combine(data, "installer-upgrade-" + Guid.NewGuid().ToString("N") + ".py");
                using (var resource = typeof(InstallerActions).Assembly.GetManifestResourceStream(
                    "SalesBuddy.UpgradeCoordinator.py"))
                {
                    if (resource == null)
                        throw new InvalidOperationException("MSI upgrade coordinator resource is missing.");
                    using (var output = File.Create(runner)) resource.CopyTo(output);
                }
            }

            public void Run(Session session, string action, string options = "")
            {
                RequireSuccess(session, Path.Combine(root, "venv", "Scripts", "python.exe"),
                    $"\"{runner}\" {action} --repo \"{root.TrimEnd('\\')}\" {options}", root);
            }

            public void Dispose()
            {
                // prepare persists the durable recovery runner alongside its snapshot.
                try { File.Delete(runner); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }
}
