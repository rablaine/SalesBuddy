"""Phase 0 probe for the prefetch-meetings backlog.

Asks WorkIQ a handful of meeting/attendee questions, times each call, and
dumps the raw response to ``logs/workiq-probe/`` for offline inspection. The
goal is to figure out what WorkIQ can actually return for attendees,
recurrence info, response status, and multi-day pulls *before* we lock in a
schema or write any prefetch service code.

Usage:
    python scripts/probe_workiq_meetings.py

Output:
    logs/workiq-probe/<timestamp>/
        00_today_table.md
        01_today_attendees_json.md
        02_today_full_detail.md
        03_week_table.md
        04_week_attendees_json.md
        summary.txt
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Allow `from app...` imports when run as a plain script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.workiq_service import query_workiq  # noqa: E402


# Each probe: (filename, description, prompt, timeout_seconds)
PROBES: list[tuple[str, str, str, int]] = []


def _build_probes() -> None:
    today = date.today().isoformat()
    week_end = (date.today() + timedelta(days=6)).isoformat()

    PROBES.extend([
        (
            "00_today_table.md",
            "Today's meetings as a markdown table (current production shape)",
            (
                f"List all my meetings on {today} in a markdown table with "
                f"columns: Time, Meeting Title, External Company. Include "
                f"ALL meetings, even internal ones."
            ),
            120,
        ),
        (
            "01_today_attendees_json.md",
            "Today's meetings with FULL attendee list, asked for as JSON",
            (
                f"List every meeting on my calendar for {today}. For each "
                f"meeting return a JSON object inside a ```json code block "
                f"with these fields: subject, start_time (ISO 8601 with "
                f"timezone), end_time, organizer_email, is_recurring, "
                f"recurrence_pattern (if any), online_meeting_url (if any), "
                f"and attendees (an array of objects with name, email, and "
                f"response_status). Wrap the whole list in a single JSON "
                f"array. Do not omit any meeting, even internal ones."
            ),
            180,
        ),
        (
            "02_today_full_detail.md",
            "Today's meetings, free-form natural language with everything",
            (
                f"For every meeting on my calendar for {today}, tell me: the "
                f"subject, start and end time, the organizer, whether it is "
                f"a recurring series (and if so, the recurrence pattern), "
                f"the Teams join URL if any, and the FULL attendee list with "
                f"each attendee's name, email address, and response status "
                f"(accepted / tentative / declined / no response). Do not "
                f"abbreviate. Do not skip internal Microsoft attendees."
            ),
            180,
        ),
        (
            "03_week_table.md",
            "Week-ahead meetings as a markdown table (latency check)",
            (
                f"List all my meetings between {today} and {week_end} in a "
                f"markdown table with columns: Date, Time, Meeting Title, "
                f"External Company. Include ALL meetings, even internal ones."
            ),
            240,
        ),
        (
            "04_week_attendees_json.md",
            "Week-ahead meetings with attendees as JSON (payload + latency)",
            (
                f"List every meeting on my calendar between {today} and "
                f"{week_end}. For each meeting return a JSON object inside a "
                f"```json code block with these fields: subject, start_time "
                f"(ISO 8601 with timezone), end_time, organizer_email, "
                f"is_recurring, attendees (array of name, email, "
                f"response_status). Wrap the whole list in a single JSON "
                f"array. Include every meeting on every day in the range."
            ),
            300,
        ),
    ])


def _run_probe(out_dir: Path, filename: str, description: str,
               prompt: str, timeout: int) -> tuple[str, float, int, str | None]:
    """Run one probe. Returns (filename, elapsed_s, response_chars, error)."""
    print(f"\n=== {filename} ===")
    print(f"    {description}")
    print(f"    timeout: {timeout}s")
    started = time.perf_counter()
    error: str | None = None
    response = ""
    try:
        response = query_workiq(prompt, timeout=timeout, operation='meeting_list')
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        print(f"    FAILED: {error}")
    elapsed = time.perf_counter() - started
    print(f"    elapsed: {elapsed:.1f}s, response chars: {len(response)}")

    body = (
        f"# {filename}\n\n"
        f"**Description:** {description}\n\n"
        f"**Elapsed:** {elapsed:.2f}s\n\n"
        f"**Response chars:** {len(response)}\n\n"
        f"**Error:** {error or 'none'}\n\n"
        f"## Prompt\n\n```\n{prompt}\n```\n\n"
        f"## Raw response\n\n```\n{response}\n```\n"
    )
    (out_dir / filename).write_text(body, encoding="utf-8")
    return filename, elapsed, len(response), error


def main() -> int:
    _build_probes()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "logs" / "workiq-probe" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing probe output to: {out_dir}")

    results: list[tuple[str, float, int, str | None]] = []
    for filename, description, prompt, timeout in PROBES:
        results.append(_run_probe(out_dir, filename, description, prompt, timeout))

    summary_lines = [
        f"WorkIQ probe run at {stamp}",
        "",
        f"{'file':<35} {'elapsed_s':>10} {'chars':>8}  status",
        f"{'-' * 35} {'-' * 10} {'-' * 8}  {'-' * 30}",
    ]
    for filename, elapsed, chars, error in results:
        status = "OK" if not error else f"ERROR: {error[:60]}"
        summary_lines.append(f"{filename:<35} {elapsed:>10.2f} {chars:>8}  {status}")
    summary_lines.append("")
    summary_lines.append("Next: open each .md file and document what WorkIQ")
    summary_lines.append("actually returned (attendee fields, recurrence info,")
    summary_lines.append("response status, JSON willingness, multi-day latency)")
    summary_lines.append("in docs/PREFETCH_MEETINGS_BACKLOG.md under Phase 0.")
    summary = "\n".join(summary_lines)
    (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)

    # Non-zero exit if every probe failed -- a single failure is still useful.
    return 0 if any(err is None for _, _, _, err in results) else 1


if __name__ == "__main__":
    sys.exit(main())
