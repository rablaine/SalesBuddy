"""Run regression coverage against the actual Electron shell source."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for shell tests')
def test_electron_startup_recovery() -> None:
    """Exercise slow readiness, failed navigation recovery, and cancellation."""
    test_file = Path(__file__).parent / 'js' / 'electron_startup.test.cjs'
    result = subprocess.run(
        ['node', '--test', str(test_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
