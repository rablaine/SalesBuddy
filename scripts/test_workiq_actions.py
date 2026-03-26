"""Quick test script to see if WorkIQ can generate daily action items.

Usage:
    & c:\dev\SalesBuddy\venv\Scripts\Activate.ps1
    python scripts/test_workiq_actions.py

Tries the same prompt Copilot uses for daily action items.
"""
import subprocess
import sys
import time


def query_workiq(question: str, timeout: int = 120) -> str:
    """Run a WorkIQ query and return the raw response."""
    cmd = f'npx -y @microsoft/workiq ask -q "{question}"'
    print(f"[workiq] Running: {cmd}")
    print(f"[workiq] Timeout: {timeout}s")
    print()

    start = time.time()
    try:
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        print(f"[workiq] Completed in {elapsed:.1f}s (exit code: {result.returncode})")
        print()

        if result.stdout:
            print("=== STDOUT ===")
            print(result.stdout)
            print()

        if result.stderr:
            # Filter out npm noise
            stderr_lines = [
                l for l in result.stderr.splitlines()
                if not l.startswith("npm") and "WARN" not in l and l.strip()
            ]
            if stderr_lines:
                print("=== STDERR (filtered) ===")
                print("\n".join(stderr_lines))
                print()

        return result.stdout

    except subprocess.TimeoutExpired:
        print(f"[workiq] TIMED OUT after {timeout}s")
        return ""


if __name__ == "__main__":
    print("=" * 60)
    print("WorkIQ Action Items Test")
    print("=" * 60)
    print()

    # The exact prompt Copilot uses
    prompt = (
        "Look through all my emails, chats, and meetings, "
        "and let me know the top three things I need to get done."
    )
    print(f"Prompt: {prompt}")
    print()

    response = query_workiq(prompt)

    if not response or not response.strip():
        print("[RESULT] No response from WorkIQ.")
    else:
        print("[RESULT] Got a response. Check above for action items.")
