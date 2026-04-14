using System;
using System.IO;
using WixToolset.Dtf.WindowsInstaller;

namespace SalesBuddy.CustomActions
{
    /// <summary>
    /// Utilities for PATH management and command detection.
    /// MSI custom actions run in a limited environment where the user's
    /// full PATH may not be available. These helpers work around that.
    /// </summary>
    public static class PathHelper
    {
        /// <summary>
        /// Reload the current process PATH from the registry.
        /// Picks up commands installed by previous steps.
        /// </summary>
        public static void RefreshPath()
        {
            var machinePath = Environment.GetEnvironmentVariable("Path",
                EnvironmentVariableTarget.Machine) ?? "";
            var userPath = Environment.GetEnvironmentVariable("Path",
                EnvironmentVariableTarget.User) ?? "";
            Environment.SetEnvironmentVariable("Path", machinePath + ";" + userPath);
        }

        /// <summary>
        /// Check if a command exists on PATH and is actually usable.
        /// For python, rejects the Windows Store stub by running --version.
        /// </summary>
        public static bool CommandExists(string command)
        {
            string fullPath = FindOnPath(command);
            if (fullPath == null) return false;

            // The Windows Store python stub lives in WindowsApps and doesn't work.
            // Also reject any python that doesn't actually respond to --version.
            if (command == "python")
            {
                if (fullPath.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0)
                    return false;
                try
                {
                    var psi = new System.Diagnostics.ProcessStartInfo
                    {
                        FileName = fullPath,
                        Arguments = "--version",
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        UseShellExecute = false,
                        CreateNoWindow = true
                    };
                    using (var p = System.Diagnostics.Process.Start(psi))
                    {
                        var output = p.StandardOutput.ReadToEnd();
                        p.WaitForExit(5000);
                        return p.ExitCode == 0 && output.Contains("Python");
                    }
                }
                catch { return false; }
            }

            return true;
        }

        /// <summary>
        /// Find the full path of a command by searching PATH directories.
        /// Checks common extensions: .exe, .cmd, .bat.
        /// </summary>
        public static string FindOnPath(string command)
        {
            if (Path.IsPathRooted(command) && File.Exists(command))
                return command;

            var extensions = new[] { "", ".exe", ".cmd", ".bat" };
            var pathDirs = (Environment.GetEnvironmentVariable("Path") ?? "").Split(';');

            foreach (var dir in pathDirs)
            {
                if (string.IsNullOrWhiteSpace(dir)) continue;
                foreach (var ext in extensions)
                {
                    var candidate = Path.Combine(dir.Trim(), command + ext);
                    if (File.Exists(candidate))
                        return candidate;
                }
            }

            return null;
        }

        /// <summary>
        /// Add a directory to the current process PATH (prepend).
        /// Optionally persist to the user's PATH in the registry.
        /// </summary>
        public static void AddToPath(string directory, bool persist = false)
        {
            var currentPath = Environment.GetEnvironmentVariable("Path") ?? "";
            if (currentPath.IndexOf(directory, StringComparison.OrdinalIgnoreCase) < 0)
            {
                Environment.SetEnvironmentVariable("Path", directory + ";" + currentPath);
            }

            if (persist)
            {
                var userPath = Environment.GetEnvironmentVariable("Path",
                    EnvironmentVariableTarget.User) ?? "";
                if (userPath.IndexOf(directory, StringComparison.OrdinalIgnoreCase) < 0)
                {
                    Environment.SetEnvironmentVariable("Path",
                        directory + ";" + userPath, EnvironmentVariableTarget.User);
                }
            }
        }

        /// <summary>
        /// Locate winget even when it's not on PATH. MSI custom actions run
        /// in a limited environment where WindowsApps may not be included.
        /// </summary>
        public static bool FindWinget(Session session)
        {
            if (CommandExists("winget")) return true;

            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);

            // Check the standard WindowsApps location
            var windowsApps = Path.Combine(localAppData, "Microsoft", "WindowsApps");
            var candidate = Path.Combine(windowsApps, "winget.exe");
            if (File.Exists(candidate))
            {
                session.Log($"Found winget at {candidate}");
                AddToPath(windowsApps);
                return true;
            }

            // Check Program Files\WindowsApps (glob equivalent)
            var programFilesWA = @"C:\Program Files\WindowsApps";
            if (Directory.Exists(programFilesWA))
            {
                try
                {
                    foreach (var dir in Directory.GetDirectories(
                        programFilesWA, "Microsoft.DesktopAppInstaller_*"))
                    {
                        var wingetPath = Path.Combine(dir, "winget.exe");
                        if (File.Exists(wingetPath))
                        {
                            session.Log($"Found winget at {wingetPath}");
                            AddToPath(dir);
                            return true;
                        }
                    }
                }
                catch (UnauthorizedAccessException)
                {
                    // Normal - WindowsApps is restricted
                }
            }

            return false;
        }

        /// <summary>
        /// Find Git even when it's not on PATH. Checks common install locations
        /// (Git for Windows, Visual Studio, GitHub Desktop, Scoop, Chocolatey).
        /// Adds the found directory to the process PATH so subsequent calls work.
        /// </summary>
        public static bool FindGit(Session session)
        {
            if (CommandExists("git")) return true;

            var programFiles = Environment.GetFolderPath(
                Environment.SpecialFolder.ProgramFiles);
            var programFilesX86 = Environment.GetFolderPath(
                Environment.SpecialFolder.ProgramFilesX86);
            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            var userProfile = Environment.GetFolderPath(
                Environment.SpecialFolder.UserProfile);

            // Common Git install locations (most likely first)
            var candidates = new[]
            {
                // Git for Windows (default install)
                Path.Combine(programFiles, "Git", "cmd"),
                Path.Combine(programFilesX86, "Git", "cmd"),
                // User-level Git for Windows
                Path.Combine(localAppData, "Programs", "Git", "cmd"),
                // Scoop
                Path.Combine(userProfile, "scoop", "shims"),
                // Chocolatey
                Path.Combine(programFiles, "chocolatey", "bin"),
                @"C:\ProgramData\chocolatey\bin",
                // Our own portable Git (installed by fallback)
                Path.Combine(localAppData, "git", "cmd"),
            };

            foreach (var dir in candidates)
            {
                var gitExe = Path.Combine(dir, "git.exe");
                if (File.Exists(gitExe))
                {
                    session.Log($"Found git at {gitExe}");
                    AddToPath(dir);
                    return true;
                }
            }

            // Visual Studio bundled Git (search versioned folders)
            var vsBase = Path.Combine(programFiles, "Microsoft Visual Studio");
            if (Directory.Exists(vsBase))
            {
                try
                {
                    foreach (var gitExe in Directory.GetFiles(
                        vsBase, "git.exe", SearchOption.AllDirectories))
                    {
                        var dir = Path.GetDirectoryName(gitExe);
                        session.Log($"Found VS-bundled git at {gitExe}");
                        AddToPath(dir);
                        return true;
                    }
                }
                catch { /* best effort */ }
            }

            return false;
        }

        /// <summary>
        /// Get the Python executable path. Prefers our installed copy
        /// at %LOCALAPPDATA%\python, falls back to PATH lookup.
        /// Rejects the Windows Store stub.
        /// </summary>
        public static string FindPython()
        {
            var localPython = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "python", "python.exe");
            if (File.Exists(localPython)) return localPython;

            var found = FindOnPath("python");
            if (found != null &&
                found.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0)
                return null;

            return found;
        }
    }
}
