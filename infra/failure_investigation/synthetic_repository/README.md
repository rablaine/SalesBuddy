# Sales Buddy failure investigation lab

This disposable repository contains a deliberate retry-delay bug. It is not the
Sales Buddy application and must not be installed or deployed.

Install the test requirements and run `pytest tests/test_upgrade.py`.
The initial test is intentionally failing: attempt 4 returns 80 seconds instead
of the documented 60-second maximum. Copilot should propose the smallest fix in
a pull request, preserve the initial delay and reject negative attempts.

Never merge automatically, deploy, add workflows, or use real diagnostic data.
