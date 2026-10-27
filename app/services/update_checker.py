"""
Update checker service for Sales Buddy.

Periodically checks if new commits are available on the remote main branch
and caches the result. Used to show an update notification in the UI.

Also fetches CHANGELOG.md from the remote and caches parsed entries so the
admin panel can show "what's new" before and after applying an update.
"""
import re
import subprocess
import threading
import time
import logging
from datetime import datetime, timezone, date

import requests

logger = logging.getLogger(__name__)

CHANGELOG_URL = (
    'https://raw.githubusercontent.com/rablaine/SalesBuddy/refs/heads/main/CHANGELOG.md'
)

# GitHub Contents API always reads from HEAD of the default branch (no CDN
# lag like raw.githubusercontent.com). Used as the primary source so the
# changelog reflects what just merged within seconds of the push.
CHANGELOG_API_URL = (
    'https://api.github.com/repos/rablaine/SalesBuddy/contents/CHANGELOG.md'
)

# Cached update state (module-level singleton)
_update_state = {
    'available': False,
    'local_commit': None,
    'remote_commit': None,
    'local_commit_full': None,
    'remote_commit_full': None,
    'commits_behind': 0,
    'last_checked': None,
    'error': None,
}

# Cached changelog state
_changelog_state = {
    'entries': [],          # list of {'date': 'YYYY-MM-DD', 'bullets': [str, ...]}
    'last_fetched': None,
    'error': None,
}

_lock = threading.Lock()


def get_update_state() -> dict:
    """Return the current cached update state."""
    with _lock:
        return dict(_update_state)


def check_for_updates() -> dict:
    """
    Run git fetch and compare local HEAD to origin/main.
    Updates the cached state and returns it.
    """
    try:
        # git fetch origin main (quiet, timeout after 15 seconds)
        subprocess.run(
            ['git', 'fetch', 'origin', 'main', '--quiet'],
            capture_output=True, text=True, timeout=15,
            cwd=_get_repo_root()
        )

        repo_root = _get_repo_root()

        # Get local HEAD commit
        local = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=repo_root
        ).stdout.strip()

        # Get remote commit
        remote = subprocess.run(
            ['git', 'rev-parse', 'origin/main'],
            capture_output=True, text=True, timeout=5, cwd=repo_root
        ).stdout.strip()

        # Count commits behind
        behind = 0
        if local != remote:
            result = subprocess.run(
                ['git', 'rev-list', '--count', f'{local}..{remote}'],
                capture_output=True, text=True, timeout=5, cwd=repo_root
            )
            behind = int(result.stdout.strip()) if result.stdout.strip() else 0

        with _lock:
            _update_state['available'] = local != remote and behind > 0
            _update_state['local_commit'] = local[:7] if local else None
            _update_state['remote_commit'] = remote[:7] if remote else None
            _update_state['local_commit_full'] = local or None
            _update_state['remote_commit_full'] = remote or None
            _update_state['commits_behind'] = behind
            _update_state['last_checked'] = datetime.now(timezone.utc).isoformat()
            _update_state['error'] = None

    except subprocess.TimeoutExpired:
        logger.warning("Update check timed out (git fetch)")
        with _lock:
            _update_state['error'] = 'timeout'
            _update_state['last_checked'] = datetime.now(timezone.utc).isoformat()
    except FileNotFoundError:
        logger.warning("Git not found -- update checking disabled")
        with _lock:
            _update_state['error'] = 'git_not_found'
            _update_state['last_checked'] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        logger.warning(f"Update check failed: {e}")
        with _lock:
            _update_state['error'] = str(e)
            _update_state['last_checked'] = datetime.now(timezone.utc).isoformat()

    return get_update_state()


def _get_repo_root() -> str:
    """Get the repository root directory."""
    import os
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

# Heading formats accepted:
#   ## M/D/YYYY - abc1234     (preferred, hash is short SHA of tagged commit)
#   ## M/D/YYYY               (untagged, falls back to date filter)
#   ## YYYY-MM-DD - abc1234   (legacy ISO date with hash)
#   ## YYYY-MM-DD             (legacy ISO date, untagged)
_DATE_HEADING_RE = re.compile(
    r'^##\s+'
    r'(?P<date>\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})'
    r'(?:\s+-\s+(?P<commit>[0-9a-f]{7,40}))?'
    r'\s*$'
)
_BULLET_RE = re.compile(r'^\s*[-*]\s+(.+?)\s*$')


def _normalize_date(raw: str) -> str:
    """Convert M/D/YYYY or YYYY-MM-DD to YYYY-MM-DD for comparison."""
    if '/' in raw:
        try:
            m, d, y = raw.split('/')
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except (ValueError, AttributeError):
            return raw
    return raw


def parse_changelog(text: str) -> list:
    """Parse CHANGELOG.md into a list of {date, commit, bullets} entries.

    Recognized heading formats (newest convention first):
        ## 4/29/2026 - abc1234
        - bullet one
        ## 4/29/2026                # untagged, will fall back to date filter
        - bullet two
        ## 2026-04-29 - abc1234     # legacy ISO date format also accepted
        - bullet three

    Each ``## ...`` heading starts a new entry, even within the same date,
    so multiple commits on the same day each get their own block.

    Returned entries include:
        date         - the raw date string as written (for display)
        date_iso     - normalized YYYY-MM-DD (for sorting / fallback compare)
        commit       - short SHA, or None if untagged
        bullets      - list of bullet text strings

    Anything outside of date sections (intro paragraphs, etc.) is ignored.
    Empty sections are dropped. Entries are returned in source order
    (the file convention is newest first).
    """
    entries = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _DATE_HEADING_RE.match(line)
        if m:
            if current and current['bullets']:
                entries.append(current)
            current = {
                'date': m.group('date'),
                'date_iso': _normalize_date(m.group('date')),
                'commit': m.group('commit'),
                'bullets': [],
            }
            continue
        if current is None:
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            current['bullets'].append(bm.group(1))
            continue
        # Continuation lines (indented under a bullet) get appended to the
        # last bullet to preserve multi-line entries.
        if line.strip() and current['bullets'] and line.startswith(' '):
            current['bullets'][-1] += ' ' + line.strip()
    if current and current['bullets']:
        entries.append(current)
    return entries


def fetch_changelog() -> dict:
    """Fetch CHANGELOG.md from the remote and update the cached state.

    Uses the GitHub Contents API with the raw media type so we always get
    the latest committed version of the file - the raw.githubusercontent.com
    CDN lags by minutes after a push, which used to cause "What you'll get"
    to come up empty right after a release.
    """
    try:
        resp = requests.get(
            CHANGELOG_API_URL,
            timeout=10,
            headers={
                'Accept': 'application/vnd.github.raw',
                'Cache-Control': 'no-cache',
                'User-Agent': 'SalesBuddy-UpdateChecker',
            },
        )
        resp.raise_for_status()
        entries = parse_changelog(resp.text)
        with _lock:
            _changelog_state['entries'] = entries
            _changelog_state['last_fetched'] = datetime.now(timezone.utc).isoformat()
            _changelog_state['error'] = None
    except Exception as e:
        logger.warning(f"Changelog fetch failed: {e}")
        with _lock:
            _changelog_state['error'] = str(e)
            _changelog_state['last_fetched'] = datetime.now(timezone.utc).isoformat()
    return get_changelog_state()


def get_changelog_state() -> dict:
    """Return the cached changelog state (copy)."""
    with _lock:
        return {
            'entries': list(_changelog_state['entries']),
            'last_fetched': _changelog_state['last_fetched'],
            'error': _changelog_state['error'],
        }


def get_commit_metadata(commits: list[str]) -> dict[str, tuple[str, str | None]]:
    """Resolve unique Git prefixes to full identities and dates, preserving unknowns."""
    metadata: dict[str, tuple[str, str | None]] = {
        commit: (
            commit.lower() if re.fullmatch(r'[0-9a-fA-F]{7,40}', commit) else commit, None,
        )
        for commit in commits
    }
    prefixes = {
        commit: commit.lower() for commit in metadata
        if re.fullmatch(r'[0-9a-fA-F]{7,40}', commit)
    }
    if not prefixes:
        return metadata
    try:
        result = subprocess.run(
            ['git', 'log', '--all', '--format=%H%x09%cs'],
            capture_output=True, text=True, timeout=5, check=True, cwd=_get_repo_root(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning('Commit date lookup failed: %s', type(exc).__name__)
        return metadata

    history = {}
    for line in result.stdout.splitlines():
        commit, separator, commit_date = line.partition('\t')
        if not separator or not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}', commit_date,
        ):
            logger.warning('Commit date lookup returned malformed Git history')
            continue
        history[commit] = commit_date
    for commit, prefix in prefixes.items():
        matches = [sha for sha in history if sha.startswith(prefix)]
        if len(matches) == 1:
            metadata[commit] = (matches[0], history[matches[0]])
        else:
            logger.info('No unique Git history match for boot commit %s', commit)
    return metadata


def get_commit_dates(commits: list[str]) -> dict[str, str | None]:
    """Resolve full or unambiguous abbreviated hashes to recorded Git commit dates."""
    return {ref: value[1] for ref, value in get_commit_metadata(commits).items()}


def commits_match(first: str, second: str) -> bool:
    """Compare identities, resolving legacy abbreviations only when unambiguous."""
    first, second = first.lower(), second.lower()
    if first == second:
        return True
    if len(first) == len(second) == 40:
        return False
    metadata = get_commit_metadata([first, second])
    return metadata[first][0] == metadata[second][0]


def is_update_dismissed(state: dict, dismissed_commit: str | None) -> bool:
    """Match saved dismissals to full update identities, including legacy prefixes."""
    remote = state.get('remote_commit_full') or state.get('remote_commit')
    return bool(remote and dismissed_commit and commits_match(dismissed_commit, remote))


def get_local_head_date() -> str | None:
    """Return the commit date (YYYY-MM-DD) of the local HEAD, or None."""
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%cs', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=_get_repo_root()
        )
        out = (result.stdout or '').strip()
        return out or None
    except Exception:
        return None


def get_commits_in_range(start_ref: str | None, end_ref: str) -> set:
    """Return a set of short SHAs (7 chars) between two git refs.

    Used to figure out which changelog entries map to commits the user
    hasn't yet pulled (or has pulled but hasn't restarted into).

    If ``start_ref`` is None or invalid, returns an empty set.
    Returns short hashes so they match the format we tag in CHANGELOG.md.
    """
    if not start_ref or not end_ref:
        return set()
    try:
        result = subprocess.run(
            ['git', 'log', f'{start_ref}..{end_ref}', '--format=%h'],
            capture_output=True, text=True, timeout=5, cwd=_get_repo_root()
        )
        if result.returncode != 0:
            return set()
        return {
            line.strip()[:7]
            for line in (result.stdout or '').splitlines()
            if line.strip()
        }
    except Exception:
        return set()


def entries_in_commits(
    entries: list,
    commits: set,
    fallback_cutoff_date: str | None,
) -> list:
    """Filter entries to those tagged with a hash in ``commits``.

    Untagged entries (legacy, no commit hash) fall back to the date filter:
    they're included if their normalized ISO date is strictly greater
    than ``fallback_cutoff_date``. This keeps older entries working
    until they roll off the bottom of the changelog.
    """
    out = []
    for e in entries:
        commit = e.get('commit')
        if commit:
            if commit[:7] in commits:
                out.append(e)
        elif fallback_cutoff_date:
            if e.get('date_iso', '') > fallback_cutoff_date:
                out.append(e)
    return out


def entries_newer_than(entries: list, cutoff_date: str | None) -> list:
    """Return entries whose date is strictly greater than cutoff_date.

    Kept for backward compatibility / display fallback. Compares using
    the normalized ISO date so M/D/YYYY and YYYY-MM-DD both work.
    If cutoff_date is None, all entries are returned.
    """
    if not cutoff_date:
        return list(entries)
    return [e for e in entries if e.get('date_iso', '') > cutoff_date]


# ---------------------------------------------------------------------------
# Electron shell rebuild detection
# ---------------------------------------------------------------------------

# A changelog bullet describing a change that requires the Electron desktop
# shell to be rebuilt (not just a git pull) starts with this marker, e.g.:
#   - *Electron Shell Update* - the desktop app can now start minimized.
# The `*...*` renders as italic to users but is a precise signal to us.
SHELL_UPDATE_MARKER = 'electron shell update'


def _bullet_flags_shell_rebuild(bullet: str) -> bool:
    """True if a changelog bullet carries the *Electron Shell Update* marker.

    Case-insensitive and tolerant of leading markdown emphasis (`*`, `_`) and
    whitespace, so `*Electron Shell Update* - ...` matches.
    """
    text = (bullet or '').strip().lstrip('*_ ').lower()
    return text.startswith(SHELL_UPDATE_MARKER)


def entries_require_shell_rebuild(entries: list, commits: set) -> bool:
    """True if any changelog entry tagged with a hash in ``commits`` carries the
    *Electron Shell Update* marker on one of its bullets.

    Used to detect ahead of time (pending range: current..remote) that an update
    will need a desktop-app rebuild, so the UI can warn and the shell can chain
    the rebuild after the pull.
    """
    for e in entries:
        commit = e.get('commit')
        if not commit or commit[:7] not in commits:
            continue
        for bullet in e.get('bullets', []):
            if _bullet_flags_shell_rebuild(bullet):
                return True
    return False


def _check_loop(interval_seconds: int) -> None:
    """Background loop that checks for updates periodically.

    Only pings git for the update badge. The changelog is lazy-loaded
    on first admin-panel open (see /api/admin/update-check) - polling it
    in the background races with GitHub's CDN right after a push and
    can pin a stale copy in cache for the whole interval.
    """
    # Small delay to let the app finish starting up, then check immediately
    time.sleep(5)
    while True:
        try:
            check_for_updates()
        except Exception as e:
            logger.error(f"Update check loop error: {e}")
        time.sleep(interval_seconds)


def start_update_checker(interval_seconds: int = 3600) -> None:
    """
    Start the background update checker thread.

    Args:
        interval_seconds: How often to check (default: 3600 = 1 hour)
    """
    thread = threading.Thread(
        target=_check_loop,
        args=(interval_seconds,),
        daemon=True,
        name='update-checker'
    )
    thread.start()
    logger.info(f"Update checker started (every {interval_seconds // 3600}h)")
