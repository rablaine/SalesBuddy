"""Run dependency-free manager report autosave, modal, and assisted picker regressions."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for JS tests')
def test_manager_report_autosave() -> None:
    """Verify autosave, modal behavior, source switching, and assisted selection limits."""
    test_file = Path(__file__).parent / 'js' / 'manager_one_on_one.test.cjs'
    result = subprocess.run(
        ['node', '--test', str(test_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
