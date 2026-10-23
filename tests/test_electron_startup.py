"""Run regression coverage against the actual Electron shell source."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for shell tests')
def test_electron_startup_recovery() -> None:
    """Exercise readiness, recovery, cancellation, and private bounded focus diagnostics."""
    test_dir = Path(__file__).parent / 'js'
    test_files = [test_dir / 'electron_startup.test.cjs',
                  test_dir / 'electron_find.test.cjs',
                  test_dir / 'electron_upgrade_safety.test.cjs',
                  test_dir / 'electron_input_focus.test.cjs']
    result = subprocess.run(
        ['node', '--test', *map(str, test_files)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
