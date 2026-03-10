# Fast parallel test runner
# Uses all CPU cores to run tests in parallel
# Usage: .\scripts\test-fast.ps1

& $PSScriptRoot\..\venv\Scripts\Activate.ps1
pytest tests/ -n auto --dist loadscope -q

# Options used:
# -n auto        = Use all available CPU cores
# --dist loadscope = Group tests by module (avoids fixture conflicts)
# -q             = Quiet output (just dots + summary)
#
# To see full output on failure, re-run failed test serially:
#   pytest tests/test_file.py::TestClass::test_name -v
