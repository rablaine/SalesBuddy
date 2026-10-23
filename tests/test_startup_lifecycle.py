"""Windows state-publication races and updater-independent desktop launches."""

import ctypes
import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import upgrade


WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='Windows file and console semantics')


def test_atomic_state_replacement_never_retries_unrelated_io_errors(tmp_path, monkeypatch):
    """Disk-full errors still fail immediately, leaving the previous complete state intact."""
    path = tmp_path / 'startup-state.json'
    upgrade.atomic_json(path, {'phase': 'starting'})
    error = OSError(errno.ENOSPC, 'synthetic full disk')

    def fail_replace(*args):
        """Refuse publication without altering either file."""
        raise error

    monkeypatch.setattr(upgrade.os, 'replace', fail_replace)
    monkeypatch.setattr(upgrade.time, 'sleep', lambda _: pytest.fail('Unexpected retry'))
    with pytest.raises(OSError) as failure:
        upgrade.atomic_json(path, {'phase': 'ready'})
    assert failure.value is error
    assert json.loads(path.read_text()) == {'phase': 'starting'}
    assert list(tmp_path.glob('*.tmp')) == []


@WINDOWS_ONLY
def test_atomic_state_retries_only_replacement_and_publishes_complete_json(
    tmp_path, monkeypatch, caplog,
):
    """Brief sharing/access/lock conflicts reuse the same fully flushed temporary file."""
    path = tmp_path / 'startup-state.json'
    upgrade.atomic_json(path, {'phase': 'starting'})
    original_replace = os.replace
    attempts = []
    pauses = []

    def replace(source, destination):
        """Model each transient Windows error before permitting actual replacement."""
        attempts.append(Path(source))
        assert json.loads(Path(source).read_text()) == {'phase': 'ready'}
        if len(attempts) <= 3:
            assert json.loads(path.read_text()) == {'phase': 'starting'}
            raise ctypes.WinError([5, 32, 33][len(attempts) - 1])
        return original_replace(source, destination)

    monkeypatch.setattr(upgrade.os, 'replace', replace)
    monkeypatch.setattr(upgrade.time, 'sleep', pauses.append)
    upgrade.atomic_json(path, {'phase': 'ready'})
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert pauses == [upgrade.STATE_WRITE_RETRY_INTERVAL_SECONDS] * 3
    assert json.loads(path.read_text()) == {'phase': 'ready'}
    assert list(tmp_path.glob('*.tmp')) == []
    assert 'retrying' in caplog.text


@WINDOWS_ONLY
def test_persistent_state_lock_has_a_bounded_budget_and_preserves_original(
    tmp_path, monkeypatch, caplog,
):
    """A permanent ACL/sharing problem remains a reported failure, not an infinite retry."""
    path = tmp_path / 'startup-state.json'
    upgrade.atomic_json(path, {'phase': 'starting'})
    clock = [0.0]
    pauses = []

    def fail_replace(*args):
        """Keep the destination locked for longer than the full retry budget."""
        raise ctypes.WinError(5)

    def sleep(seconds):
        """Advance a deterministic clock without delaying the test."""
        pauses.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(upgrade.os, 'replace', fail_replace)
    monkeypatch.setattr(upgrade.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(upgrade.time, 'sleep', sleep)
    with pytest.raises(PermissionError) as failure:
        upgrade.atomic_json(path, {'phase': 'ready'})
    assert failure.value.winerror == 5
    assert sum(pauses) == pytest.approx(upgrade.STATE_WRITE_RETRY_SECONDS)
    assert len(pauses) <= 41
    assert json.loads(path.read_text()) == {'phase': 'starting'}
    assert list(tmp_path.glob('*.tmp')) == []
    assert 'remained blocked after bounded retries' in caplog.text


def windows_kernel():
    """Declare only the native APIs used by the real Windows regression."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@WINDOWS_ONLY
def test_real_windows_reader_does_not_kill_supervisor_status_publication(tmp_path):
    """Exercise actual supervisor publication with a real non-delete-sharing Windows reader."""
    from app.supervisor import StartupStatus

    kernel = windows_kernel()
    writer = StartupStatus(tmp_path, 'isolated-boot', 'a' * 40)
    writer.publish('starting', 'starting', [])
    handle = kernel.CreateFileW(str(writer.path), 0x80000000, 1 | 2, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    released = threading.Event()

    def release():
        """Release the reader shortly after publication first encounters the actual lock."""
        kernel.CloseHandle(handle)
        released.set()

    timer = threading.Timer(0.15, release)
    timer.start()
    try:
        writer.publish('ready', 'ready', [])
        assert released.is_set()
        assert json.loads(writer.path.read_text())['state'] == 'ready'
    finally:
        timer.join()


@pytest.mark.parametrize('desktop', [True, False])
def test_workspace_launch_does_not_inherit_updater_console_or_output(tmp_path, monkeypatch, desktop):
    """Normal restored desktop and headless fallback both use independent process ownership."""
    if desktop:
        shell = tmp_path / 'electron-dist' / 'Sales Buddy.exe'
        shell.parent.mkdir()
        shell.touch()
    calls = []

    def popen(argv, **options):
        """Inspect still-open handles and the real launch options without starting an app."""
        assert options['stdin'] == subprocess.DEVNULL
        assert options['stderr'] == subprocess.STDOUT
        assert options['stdout'].name == str(tmp_path / 'logs' / 'workspace-launch.log')
        assert not options['stdout'].closed
        calls.append((argv, options))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    upgrade.launch_workspace(tmp_path)
    assert len(calls) == 1
    argv, options = calls[0]
    assert options['close_fds'] is True and options['cwd'] == tmp_path
    assert argv == ([str(shell)] if desktop else [
        str(upgrade._python(tmp_path / 'venv')), '-m', 'app.supervisor',
    ])
    if os.name == 'nt':
        assert options['creationflags'] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        assert options['start_new_session'] is True


def test_independent_desktop_launch_failure_is_not_hidden(tmp_path, monkeypatch):
    """A denied breakaway or executable access fails explicitly instead of launching attached."""
    shell = tmp_path / 'electron-dist' / 'Sales Buddy.exe'
    shell.parent.mkdir()
    shell.touch()
    calls = []

    def refuse(*args, **kwargs):
        """Refuse the native launch; no attached fallback is allowed."""
        calls.append(args)
        raise PermissionError('synthetic process policy refusal')

    monkeypatch.setattr(subprocess, 'Popen', refuse)
    with pytest.raises(PermissionError, match='process policy refusal'):
        upgrade.launch_desktop(tmp_path)
    assert len(calls) == 1


def test_launch_desktop_cli_uses_shared_launcher(tmp_path, monkeypatch):
    """PowerShell and shell-rebuild recovery can share the standard-library-only launcher."""
    calls = []
    monkeypatch.setattr(upgrade, 'launch_desktop', calls.append)
    monkeypatch.setattr(sys, 'argv', [
        'upgrade.py', '--repo', str(tmp_path), 'launch-desktop',
    ])
    assert upgrade.main() == 0
    assert calls == [tmp_path.resolve()]


CONSOLE_PARENT = r"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.upgrade import _launch_independent, launch_desktop
root, control, kind = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
class BasicLimits(ctypes.Structure):
    _fields_ = [
        ('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
        ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
        ('scheduling', wintypes.DWORD),
    ]
class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
class Limits(ctypes.Structure):
    _fields_ = [
        ('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
        ('peak_job', ctypes.c_size_t),
    ]
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.GetConsoleWindow.restype = wintypes.HWND
kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
job = kernel.CreateJobObjectW(None, None)
limits = Limits()
limits.basic.flags = 0x2000 | 0x800
if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
    raise ctypes.WinError(ctypes.get_last_error())
if kind == 'electron':
    launch_desktop(root)
else:
    code = (
        'import json,os,time; from pathlib import Path; '
        'folder=Path(os.environ["SALESBUDDY_TEST_LIFETIME"]); '
        'temporary=folder/"ready.tmp"; '
        'temporary.write_text(json.dumps(dict(pid=os.getpid()))); '
        'temporary.replace(folder/"ready.json"); '
        '\nwhile not (folder/"stop").exists():\n'
        '    (folder/"heartbeat").touch(); time.sleep(0.05)\n'
    )
    _launch_independent(root, [sys._base_executable, '-c', code])
deadline = time.monotonic() + 25
while not (control/'ready.json').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Independent child never became ready')
    time.sleep(0.05)
pid = json.loads((control/'ready.json').read_text())['pid']
process = kernel.OpenProcess(0x1000, False, pid)
if not process:
    raise ctypes.WinError(ctypes.get_last_error())
member = wintypes.BOOL()
try:
    if not kernel.IsProcessInJob(process, job, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    kernel.CloseHandle(process)
pids = (wintypes.DWORD * 256)()
count = kernel.GetConsoleProcessList(pids, 256)
if not count or count > 256:
    raise ctypes.WinError(ctypes.get_last_error())
window = kernel.GetConsoleWindow()
if not window:
    raise RuntimeError('Isolated parent has no console to close')
(control/'parent.json').write_text(json.dumps(dict(
    window=window, child_pid=pid, console_pids=list(pids)[:count], in_parent_job=bool(member.value),
)))
while True:
    time.sleep(0.1)
"""


def _stage_windowless_electron_fixture(repo: Path, control: Path) -> None:
    """Copy only a complete Electron runtime, omitting the real application and all user data."""
    root = Path(__file__).parents[1]
    candidates = [root / 'electron' / 'dist' / 'win-unpacked', root / 'electron-dist']
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(Path(os.environ['LOCALAPPDATA']) / 'SalesBuddy' / 'electron-dist')
    source = next((candidate for candidate in candidates if all(
        (candidate / name).is_file() for name in ('Sales Buddy.exe', 'icudtl.dat', 'resources.pak')
    )), None)
    if source is None:
        pytest.skip('An existing packaged Electron runtime is required for the GUI regression')
    destination = repo / 'electron-dist'

    def ignore(directory, names):
        """Do not copy or execute the packaged product application."""
        return {'app.asar', 'app.asar.unpacked', 'app'} & set(names) if (
            Path(directory) == source / 'resources'
        ) else set()

    shutil.copytree(source, destination, ignore=ignore)
    application = destination / 'resources' / 'app'
    application.mkdir()
    (application / 'package.json').write_text(json.dumps({
        'name': 'salesbuddy-lifecycle-regression', 'version': '1.0.0', 'main': 'main.js',
    }))
    (application / 'main.js').write_text("""
const {app} = require('electron');
const fs = require('fs');
const path = require('path');
const folder = process.env.SALESBUDDY_TEST_LIFETIME;
app.setPath('userData', path.join(folder, 'user-data'));
app.disableHardwareAcceleration();
app.whenReady().then(() => {
  const temporary = path.join(folder, 'ready.tmp');
  fs.writeFileSync(temporary, JSON.stringify({pid: process.pid}));
  fs.renameSync(temporary, path.join(folder, 'ready.json'));
  const timer = setInterval(() => {
    if (fs.existsSync(path.join(folder, 'stop'))) {
      clearInterval(timer);
      app.quit();
    } else {
      fs.writeFileSync(path.join(folder, 'heartbeat'), String(Date.now()));
    }
  }, 50);
});
""", encoding='utf-8')


@WINDOWS_ONLY
@pytest.mark.parametrize('kind', ['python', 'electron'])
def test_real_child_survives_closing_its_updater_console_and_kill_on_close_job(
    tmp_path, kind, request,
):
    """Close a real hidden console, destroying its job, while the relaunched app keeps running."""
    repo = tmp_path / 'isolated install'
    repo.mkdir()
    control = tmp_path / 'control'
    control.mkdir()
    if kind == 'electron':
        _stage_windowless_electron_fixture(repo, control)
        request.addfinalizer(lambda: shutil.rmtree(repo / 'electron-dist'))
    environment = {
        **os.environ, 'SALESBUDDY_TEST_LIFETIME': str(control),
        '__PYVENV_LAUNCHER__': sys.executable,
    }
    environment.pop('ELECTRON_RUN_AS_NODE', None)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    parent = subprocess.Popen(
        [sys._base_executable, '-c', CONSOLE_PARENT, str(Path(__file__).parents[1]),
         str(repo), str(control), kind],
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
    )
    kernel = windows_kernel()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    child_handle = None
    try:
        deadline = time.monotonic() + 30
        while not (control / 'parent.json').exists():
            if parent.poll() is not None:
                stdout, stderr = parent.communicate()
                pytest.fail(f'Isolated console parent exited: {stdout!r} {stderr!r}')
            assert time.monotonic() < deadline, 'No console identity was published'
            time.sleep(0.05)
        identity = json.loads((control / 'parent.json').read_text())
        child_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, identity['child_pid'])
        assert child_handle
        assert not identity['in_parent_job']
        assert identity['child_pid'] not in identity['console_pids']
        heartbeat = control / 'heartbeat'
        while not heartbeat.exists():
            assert time.monotonic() < deadline, 'Independent child did not start its heartbeat'
            time.sleep(0.05)
        before = heartbeat.stat().st_mtime_ns
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        assert user.PostMessageW(identity['window'], 0x0010, 0, 0)
        parent.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 0) == 258
        time.sleep(0.2)
        assert heartbeat.stat().st_mtime_ns > before
    finally:
        (control / 'stop').touch()
        if child_handle:
            try:
                if kernel.WaitForSingleObject(child_handle, 10000) == 258:
                    kernel.TerminateProcess(child_handle, 1)
                    kernel.WaitForSingleObject(child_handle, 5000)
            finally:
                kernel.CloseHandle(child_handle)
        if parent.poll() is None:
            parent.terminate()
        parent.communicate(timeout=10)
