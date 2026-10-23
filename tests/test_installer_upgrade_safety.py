"""MSI upgrade source contracts, without invoking WiX or a real installation."""

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
CUSTOM_ACTIONS = ROOT / "installer" / "SalesBuddy.CustomActions"


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    """Read the installer contracts without importing application code."""
    return {
        "actions": (CUSTOM_ACTIONS / "CustomActions.cs").read_text(encoding="utf-8"),
        "safety": (CUSTOM_ACTIONS / "UpgradeSafety.cs").read_text(encoding="utf-8"),
        "project": (CUSTOM_ACTIONS / "SalesBuddy.CustomActions.csproj").read_text(
            encoding="utf-8"
        ),
    }


def section(source: str, start: str, end: str) -> str:
    """Select a bounded method section without interpreting C# or PowerShell."""
    return source.split(start, 1)[1].split(end, 1)[0]


def test_existing_install_routes_before_bootstrap(sources: dict[str, str]) -> None:
    """Existing installations cannot reach fresh bootstrap, pip or Flask setup."""
    action = section(sources["actions"], "public static ActionResult InstallAction",
                     "public static ActionResult UninstallAction")
    gate = action.index("if (existingInstall)")
    for bootstrap in (
        "EnsureWinget(session)",
        "EnsureGit(session)",
        "EnsurePython(session)",
        "EnsureAzureCliWithProgress(session",
        "EnsureNodeJs(session)",
    ):
        assert gate < action.index(bootstrap)
    assert "return UpgradeExistingInstallation(" in action[gate:]
    assert "HasExistingInstallation(installDir)" in action
    guarded = section(sources["safety"], "private static ActionResult UpgradeExistingInstallation",
                      "private static void RequireSuccess")
    for bootstrap in (
        "EnsurePython(",
        "EnsureWinget(",
        "EnsureGit(",
        "EnsureAzureCli",
        "EnsureNodeJs(",
        "InstallViaWinget(",
        "SetupPythonEnv(",
        "FindPython(",
    ):
        assert bootstrap not in guarded
    assert 'Path.Combine(root, "venv", "Scripts", "python.exe")' in guarded


def test_partial_install_is_never_treated_as_fresh(sources: dict[str, str]) -> None:
    """Missing git metadata must not permit a destructive fresh-install repair."""
    detector = section(sources["safety"], "private static bool HasExistingInstallation",
                       "private static ActionResult UpgradeExistingInstallation")
    for marker in (".git", "app", "venv", "electron-dist", "data", ".env", "data-path.txt"):
        assert f'"{marker}"' in detector
    assert 'File.Exists(Path.Combine(root, ".git"))' in detector


def test_no_discard_or_nuclear_existing_upgrade(sources: dict[str, str]) -> None:
    """The installer never resets, cleans or force-checks out existing source."""
    for source in (sources["actions"], sources["safety"]):
        for forbidden in ("reset --hard", "clean -fd", "checkout -f", "stash drop"):
            assert forbidden not in source
    guarded = section(sources["safety"], "private static ActionResult UpgradeExistingInstallation",
                      "private static void RequireSuccess")
    for forbidden in ("CloneOrUpdateRepo(", "SetupPythonEnv(", "ConfigureApp(",
                      "ForceDeleteDirectory(", "RestoreDatabase", "StartServer("):
        assert forbidden not in guarded
    fresh = section(sources["actions"], "private static void CloneOrUpdateRepo",
                    "private static void CheckoutRef")
    assert fresh.index("HasExistingInstallation") < fresh.index('"git", "init"')
    assert "throw new InvalidOperationException" in fresh


def test_guarded_upgrade_sequence_and_prerequisites(sources: dict[str, str]) -> None:
    """The stopped, valid old runtime prepares snapshots before any source mutation."""
    guarded = section(sources["safety"], "private static ActionResult UpgradeExistingInstallation",
                      "private static void RequireSuccess")
    checkpoints = (
        '"db_paths.py"',
        'RequireSuccess(session, python, "-c \\"import dotenv\\""',
        'RequireSuccess(session, "git", "rev-parse --verify HEAD"',
        "StopRunningApp(session, root)",
        '"prepare"',
        '"apply"',
        "RunElectronSetup(",
        "LaunchAndVerify(",
        '"Installation complete!"',
    )
    positions = [guarded.index(checkpoint) for checkpoint in checkpoints]
    assert positions == sorted(positions)
    assert "--target origin/{repoRef} --shell-rebuild" in guarded
    assert "desktopShell: true, accept: true" in guarded


def test_coordinator_is_embedded_for_first_safety_rollout(sources: dict[str, str]) -> None:
    """An old checkout need not already contain upgrade.py to get a safe snapshot."""
    project = ET.fromstring(sources["project"])
    resource = project.find(".//EmbeddedResource")
    assert resource is not None
    assert resource.attrib["Include"] == r"..\..\scripts\upgrade.py"
    assert resource.attrib["LogicalName"] == "SalesBuddy.UpgradeCoordinator.py"
    runner = section(sources["safety"], "private sealed class UpgradeCoordinator", "\0")
    assert 'GetManifestResourceStream(' in runner
    assert '"SalesBuddy.UpgradeCoordinator.py"' in runner
    assert '"installer-upgrade-"' in runner
    assert '"venv", "Scripts", "python.exe"' in runner
    assert '--repo \\"{root.TrimEnd' in runner
    assert "File.Delete(runner)" in runner


def test_shell_failure_never_qualifies_by_stale_executable(sources: dict[str, str]) -> None:
    """A nonzero shell build cannot be hidden by an old staged executable."""
    build = section(sources["actions"], "private static bool RunElectronSetup",
                    "private static bool IsShellRunning")
    assert "-SkipPull -NoLaunch" in build
    assert '" -Rebuild"' in build
    assert "bool ok = exitCode == 0 && File.Exists(exe)" in build
    assert build.count("ProcessRunner.Run(") == 1
    guarded = section(sources["safety"], "private static ActionResult UpgradeExistingInstallation",
                      "private static void RequireSuccess")
    assert 'phase = "shell"' in guarded
    assert 'phase = "startup"' in guarded
    assert 'coordinator.Run(session, "fail", "--phase " + phase)' in guarded
    assert '"restore"' not in guarded


def test_new_boot_identity_commit_and_worker_before_accept(sources: dict[str, str]) -> None:
    """An old ready file or merely open HTTP port cannot qualify as completion."""
    launch = section(sources["safety"], "private static void LaunchAndVerify",
                     "private sealed class UpgradeCoordinator")
    for contract in (
        "InstalledCommit(root)",
        '"startup-state.json"',
        "File.Delete(statePath)",
        "File.GetLastWriteTimeUtc(statePath) >= launchedAt",
        '"expected_commit"',
        "expected, StringComparison.Ordinal",
        "candidate != previousBoot",
        "Guid.TryParse(candidate",
        'EnvironmentVariables["SALESBUDDY_BOOT_ID"]',
    ):
        assert contract in launch
    assert launch.index("File.Delete(statePath)") < launch.index("Process.Start(info)")
    assert launch.index('"wait-ready"') < launch.index('"accept"')
    assert "if (accept)" in launch
    assert "--boot-id {boot} --port {GetPortFromEnv(root)}" in launch


def test_fresh_install_also_waits_before_completion(sources: dict[str, str]) -> None:
    """The first-install web fallback still needs a fresh supervised worker."""
    action = section(sources["actions"], "public static ActionResult InstallAction",
                     "public static ActionResult UninstallAction")
    assert "LaunchAndVerify(session, installDir, coordinator, electronOk, accept: false)" in action
    assert action.index("LaunchAndVerify(") < action.index('"Installation complete!"')
    assert "StartServer(session, installDir)" not in action
    configure = section(sources["actions"], "private static void ConfigureApp",
                        "private static void CreateShortcuts")
    assert "create_app" not in configure
    assert "run_migrations" not in configure


def test_process_stop_is_verified_and_scoped(sources: dict[str, str]) -> None:
    """Unknown ownership, stop failures and locked shells abort before snapshots."""
    stop = section(sources["actions"], "private static void StopRunningApp",
                   "private static Process[] SafeGetProcesses")
    for contract in (
        "Get-CimInstance Win32_Process -ErrorAction Stop",
        "Cannot establish process ownership",
        "ParentProcessId",
        "Stop-Process -Id $item.ProcessId",
        "WaitForExit(5000)",
        "if (@(Get-InstallProcesses).Count)",
        "if (exitCode != 0)",
        "FileShare.None",
    ):
        assert contract in stop
    assert "Stop-Process -Name" not in stop
    assert "TryKill(" not in stop
    assert "throw new InvalidOperationException" in stop


def test_mandatory_process_exit_codes_are_fatal(sources: dict[str, str]) -> None:
    """The shared wrapper and fresh-install dependency setup cannot ignore failures."""
    required = section(sources["safety"], "private static void RequireSuccess",
                       "private static string InstalledCommit")
    assert "if (code != 0)" in required
    assert "throw new InvalidOperationException" in required
    setup = section(sources["actions"], "private static void SetupPythonEnv",
                    "private static bool VenvImportsFlask")
    assert 'RequireSuccess(session, pythonExe, $"-m venv' in setup
    assert 'RequireSuccess(session, pipExe, $"install -r' in setup
    assert "Backend dependencies failed verification" in setup
