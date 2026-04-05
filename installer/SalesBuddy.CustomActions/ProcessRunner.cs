using System;
using System.Diagnostics;
using System.Text;
using WixToolset.Dtf.WindowsInstaller;

namespace SalesBuddy.CustomActions
{
    /// <summary>
    /// Runs external commands with output capture and MSI UI status updates.
    /// All commands run with CreateNoWindow=true so no terminal windows appear.
    /// </summary>
    public static class ProcessRunner
    {
        private static DateTime _lastStatusUpdate = DateTime.MinValue;

        /// <summary>
        /// Run a command and capture output to the MSI log.
        /// </summary>
        /// <param name="session">MSI session for logging.</param>
        /// <param name="fileName">Executable to run.</param>
        /// <param name="arguments">Command-line arguments.</param>
        /// <param name="workingDirectory">Working directory for the process.</param>
        /// <param name="livePrefix">If set, update MSI status text as
        /// "{livePrefix} {line}".</param>
        /// <param name="liveThrottleMs">Minimum ms between status updates.
        /// 0 = every line (good for spinners), 500 = twice per second.</param>
        /// <returns>Process exit code.</returns>
        public static int Run(
            Session session,
            string fileName,
            string arguments,
            string workingDirectory = null,
            string livePrefix = null,
            int liveThrottleMs = 0)
        {
            var psi = new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = arguments,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };

            if (!string.IsNullOrEmpty(workingDirectory))
                psi.WorkingDirectory = workingDirectory;

            session.Log($"[CMD] {fileName} {arguments}");

            using (var process = Process.Start(psi))
            {
                // Read stderr asynchronously to prevent deadlock when both
                // stdout and stderr buffers fill simultaneously.
                var stderr = new StringBuilder();
                process.ErrorDataReceived += (sender, e) =>
                {
                    if (e.Data != null) stderr.AppendLine(e.Data);
                };
                process.BeginErrorReadLine();

                while (!process.StandardOutput.EndOfStream)
                {
                    var line = process.StandardOutput.ReadLine();
                    if (string.IsNullOrWhiteSpace(line)) continue;

                    session.Log(line);

                    if (livePrefix != null)
                    {
                        var throttle = TimeSpan.FromMilliseconds(liveThrottleMs);
                        bool throttleOk = throttle == TimeSpan.Zero
                            || (DateTime.UtcNow - _lastStatusUpdate) >= throttle;

                        if (throttleOk && IsDisplayableLine(line))
                        {
                            var detail = line.Length > 60
                                ? line.Substring(0, 57) + "..."
                                : line;
                            UpdateStatus(session, $"{livePrefix} {detail}");
                            _lastStatusUpdate = DateTime.UtcNow;
                        }
                    }
                }

                process.WaitForExit();

                if (stderr.Length > 0)
                    session.Log($"[STDERR] {stderr}");

                session.Log($"[EXIT] {process.ExitCode}");
                return process.ExitCode;
            }
        }

        /// <summary>
        /// Run a PowerShell script block without showing a terminal window.
        /// Writes the script to a temp file and executes it with -File.
        /// </summary>
        /// <param name="session">MSI session for logging.</param>
        /// <param name="script">PowerShell script content.</param>
        /// <param name="livePrefix">If set, update MSI status text.</param>
        /// <param name="liveThrottleMs">Minimum ms between status updates.</param>
        /// <returns>Process exit code.</returns>
        public static int RunPowerShell(
            Session session,
            string script,
            string livePrefix = null,
            int liveThrottleMs = 0)
        {
            var tempScript = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                $"SalesBuddy-{Guid.NewGuid():N}.ps1");

            System.IO.File.WriteAllText(tempScript, script);
            try
            {
                return Run(session, "powershell.exe",
                    $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{tempScript}\"",
                    livePrefix: livePrefix, liveThrottleMs: liveThrottleMs);
            }
            finally
            {
                try { System.IO.File.Delete(tempScript); }
                catch { /* best effort cleanup */ }
            }
        }

        /// <summary>
        /// Returns true if the line is worth showing in the MSI UI.
        /// Always filters out unicode progress bars and download counters.
        /// </summary>
        private static bool IsDisplayableLine(string line)
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0) return false;

            // Allow single-char spinner frames: / - \ |
            if (trimmed.Length == 1 && (trimmed[0] == '/' || trimmed[0] == '-'
                || trimmed[0] == '\\' || trimmed[0] == '|'))
                return true;

            // Filter unicode block characters (winget progress bars: \u2588 \u2591 \u2592 \u2593)
            foreach (char c in trimmed)
            {
                if (c == '\u2588' || c == '\u2591' || c == '\u2592' || c == '\u2593')
                    return false;
            }

            // Filter download size lines like "1024 KB / 2.77 MB"
            if (trimmed.Contains("KB /") || trimmed.Contains("MB /")
                || trimmed.Contains("GB /"))
                return false;

            // Filter percentage-only lines like "3%" or "100%"
            if (trimmed.EndsWith("%") && trimmed.Length <= 5)
                return false;

            // Lines that are just ASCII progress bars
            if (trimmed.StartsWith("[") && trimmed.Contains("=")) return false;

            // Must contain at least one letter or digit to be meaningful
            foreach (char c in trimmed)
            {
                if (char.IsLetterOrDigit(c)) return true;
            }
            return false;
        }

        /// <summary>
        /// Update the status text on the MSI progress page.
        /// Sends ActionStart so the ActionText control updates (WixUI_Minimal
        /// does not have an ActionData text control).
        /// </summary>
        public static void UpdateStatus(Session session, string message)
        {
            using (var record = new Record(3))
            {
                record[1] = "InstallAction";
                record[2] = message;
                record[3] = "[1]";
                session.Message(InstallMessage.ActionStart, record);
            }

            // Re-establish progress context after ActionStart, otherwise
            // the engine ignores subsequent Type 2 (increment) messages.
            using (var actionInfo = new Record(4))
            {
                actionInfo[1] = 1; // Type 1 = action info
                actionInfo[2] = 0; // 0 ticks per ActionData (we advance manually)
                actionInfo[3] = 0; // unused
                actionInfo[4] = 0; // unused
                session.Message(InstallMessage.Progress, actionInfo);
            }
        }
    }
}
