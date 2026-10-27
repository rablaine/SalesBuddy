"""Tests for Git metadata, changelog parsing and entry filtering in update_checker."""
import subprocess
from unittest.mock import Mock, patch

import pytest

from app.services.update_checker import (
    parse_changelog, entries_newer_than, entries_in_commits, get_commit_dates,
    get_commit_metadata, commits_match, check_for_updates,
    is_update_dismissed,
)


class TestCommitIdentities:
    def test_versions_resolve_to_full_identity(self):
        """Seven, eight and forty characters identify the same unique commit."""
        sha = '2abcc4507bed30c94061059fc6c4812668264229'
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout=f'{sha}\t2026-10-26\n',
        )):
            metadata = get_commit_metadata([sha, sha[:7], sha[:8].upper()])
            assert set(metadata.values()) == {(sha, '2026-10-26')}
            assert commits_match(sha[:8], sha[:7])

    def test_colliding_prefixes_are_not_equal_to_a_full_hash(self):
        """A shared display prefix cannot prove two commits are identical."""
        first, second = 'abcdef01' + '1' * 32, 'abcdef02' + '2' * 32
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout=f'{first}\t2026-10-25\n{second}\t2026-10-25\n',
        )):
            assert not commits_match(first, second)
            assert not commits_match('abcdef0', first)
            assert get_commit_metadata(['abcdef0']) == {'abcdef0': ('abcdef0', None)}

    def test_update_state_retains_full_identities_and_fixed_short_labels(self, monkeypatch):
        """Git's default abbreviation length must not become an update identity."""
        from app.services import update_checker
        monkeypatch.setattr(update_checker, '_update_state', {})
        sha = '2abcc4507bed30c94061059fc6c4812668264229'
        with patch('app.services.update_checker.subprocess.run', side_effect=[
            Mock(stdout=''), Mock(stdout=sha), Mock(stdout=sha),
        ]):
            state = check_for_updates()
        assert state['local_commit'] == state['remote_commit'] == '2abcc45'
        assert state['local_commit_full'] == state['remote_commit_full'] == sha
        assert state['available'] is False

    def test_dismissal_matches_legacy_identity_but_not_colliding_full_commit(self):
        """A dismissal survives hash-length changes without hiding a different release."""
        first, second = 'abcdef01' + '1' * 32, 'abcdef02' + '2' * 32
        state = {'remote_commit': first[:7], 'remote_commit_full': first}
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout=f'{first}\t2026-10-25\n{second}\t2026-10-25\n',
        )):
            assert is_update_dismissed(state, first)
            assert is_update_dismissed(state, first[:8])
            assert not is_update_dismissed(state, second)
            assert not is_update_dismissed(state, first[:7])
            assert not is_update_dismissed(state, None)


class TestCommitDates:
    def test_resolves_full_and_unique_short_hashes_in_one_local_read(self):
        """Legacy short versions and full boot hashes resolve to the same recorded date."""
        sha = 'abcdef01' + '2' * 32
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout=f'{sha}\t2026-10-26\n',
        )) as run:
            result = get_commit_dates([sha, 'abcdef0', 'ABCDEF01'])
        assert result == dict.fromkeys([sha, 'abcdef0', 'ABCDEF01'], '2026-10-26')
        run.assert_called_once()
        assert run.call_args.args[0] == ['git', 'log', '--all', '--format=%H%x09%cs']
        assert run.call_args.kwargs['check'] is True
        assert run.call_args.kwargs['timeout'] == 5

    def test_ambiguous_missing_and_unknown_hashes_have_no_invented_date(self):
        """Even same-day prefix collisions must not be assigned an arbitrary commit."""
        first, second = 'abcdef01' + '1' * 32, 'abcdef02' + '2' * 32
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout=f'{first}\t2026-10-25\n{second}\t2026-10-25\n',
        )):
            result = get_commit_dates(['abcdef0', '1234567', 'unknown', first])
        assert result == {
            'abcdef0': None, '1234567': None, 'unknown': None, first: '2026-10-25',
        }

    @pytest.mark.parametrize('commits', [[], ['unknown', '--all', 'HEAD', 'abc123']])
    def test_invalid_versions_do_not_launch_git(self, commits):
        """Telemetry version strings are hashes, never Git revision expressions or flags."""
        with patch('app.services.update_checker.subprocess.run') as run:
            assert get_commit_dates(commits) == dict.fromkeys(commits)
        run.assert_not_called()

    @pytest.mark.parametrize('failure', [
        FileNotFoundError('git'),
        subprocess.TimeoutExpired('git', 5),
        subprocess.CalledProcessError(1, 'git'),
    ])
    def test_lookup_failures_are_logged_and_leave_dates_explicitly_unknown(self, failure, caplog):
        """Missing history must not hide version counts or manufacture a release date."""
        with patch('app.services.update_checker.subprocess.run', side_effect=failure):
            assert get_commit_dates(['abcdef0']) == {'abcdef0': None}
        assert 'Commit date lookup failed' in caplog.text

    def test_malformed_git_output_is_logged(self, caplog):
        """Unexpected Git output is surfaced without interpreting it as a date."""
        with patch('app.services.update_checker.subprocess.run', return_value=Mock(
            stdout='unexpected output\n',
        )):
            assert get_commit_dates(['abcdef0']) == {'abcdef0': None}
        assert 'malformed Git history' in caplog.text


class TestParseChangelog:
    def test_parses_single_section(self):
        text = """# Changelog

## 2026-04-28
- First change
- Second change
"""
        entries = parse_changelog(text)
        assert len(entries) == 1
        assert entries[0]['date'] == '2026-04-28'
        assert entries[0]['date_iso'] == '2026-04-28'
        assert entries[0]['commit'] is None
        assert entries[0]['bullets'] == ['First change', 'Second change']

    def test_parses_multiple_sections(self):
        text = """## 2026-04-28
- Newer change

## 2026-04-21
- Older change one
- Older change two
"""
        entries = parse_changelog(text)
        assert len(entries) == 2
        assert entries[0]['date'] == '2026-04-28'
        assert entries[1]['date'] == '2026-04-21'
        assert entries[1]['bullets'] == ['Older change one', 'Older change two']

    def test_parses_us_date_format(self):
        text = """## 4/29/2026
- Some change
"""
        entries = parse_changelog(text)
        assert len(entries) == 1
        assert entries[0]['date'] == '4/29/2026'
        assert entries[0]['date_iso'] == '2026-04-29'
        assert entries[0]['commit'] is None

    def test_parses_date_with_commit_hash(self):
        text = """## 4/29/2026 - a3547a9
- A change tied to a specific commit
"""
        entries = parse_changelog(text)
        assert len(entries) == 1
        assert entries[0]['date'] == '4/29/2026'
        assert entries[0]['date_iso'] == '2026-04-29'
        assert entries[0]['commit'] == 'a3547a9'

    def test_parses_iso_date_with_commit_hash(self):
        text = """## 2026-04-29 - eb0ed08
- Legacy ISO date with hash also works
"""
        entries = parse_changelog(text)
        assert entries[0]['commit'] == 'eb0ed08'
        assert entries[0]['date_iso'] == '2026-04-29'

    def test_multiple_entries_same_date_different_commits(self):
        text = """## 4/29/2026 - aaa1111
- First commit on this date

## 4/29/2026 - bbb2222
- Second commit on the same date
"""
        entries = parse_changelog(text)
        assert len(entries) == 2
        assert entries[0]['commit'] == 'aaa1111'
        assert entries[1]['commit'] == 'bbb2222'
        assert entries[0]['date_iso'] == entries[1]['date_iso'] == '2026-04-29'

    def test_ignores_intro_text(self):
        text = """# Changelog

Some intro paragraph that should be ignored.

## 2026-04-28
- Real change
"""
        entries = parse_changelog(text)
        assert len(entries) == 1
        assert entries[0]['bullets'] == ['Real change']

    def test_drops_empty_sections(self):
        text = """## 2026-04-28

## 2026-04-21
- Has a bullet
"""
        entries = parse_changelog(text)
        assert len(entries) == 1
        assert entries[0]['date'] == '2026-04-21'

    def test_supports_asterisk_bullets(self):
        text = """## 2026-04-28
* Asterisk bullet
"""
        entries = parse_changelog(text)
        assert entries[0]['bullets'] == ['Asterisk bullet']

    def test_appends_indented_continuation_to_last_bullet(self):
        text = """## 2026-04-28
- First line
  continued on second line
- Second bullet
"""
        entries = parse_changelog(text)
        assert entries[0]['bullets'] == [
            'First line continued on second line',
            'Second bullet',
        ]

    def test_empty_input(self):
        assert parse_changelog('') == []

    def test_no_date_headings(self):
        assert parse_changelog('# Changelog\n\nSome text only.') == []


class TestEntriesNewerThan:
    def _entries(self):
        return [
            {'date': '2026-04-28', 'date_iso': '2026-04-28', 'commit': None, 'bullets': ['c']},
            {'date': '2026-04-21', 'date_iso': '2026-04-21', 'commit': None, 'bullets': ['b']},
            {'date': '2026-04-14', 'date_iso': '2026-04-14', 'commit': None, 'bullets': ['a']},
        ]

    def test_filters_strictly_newer(self):
        result = entries_newer_than(self._entries(), '2026-04-21')
        assert [e['date'] for e in result] == ['2026-04-28']

    def test_returns_all_when_cutoff_none(self):
        result = entries_newer_than(self._entries(), None)
        assert len(result) == 3

    def test_returns_all_when_cutoff_empty(self):
        result = entries_newer_than(self._entries(), '')
        assert len(result) == 3

    def test_returns_empty_when_cutoff_newer_than_all(self):
        result = entries_newer_than(self._entries(), '2026-12-31')
        assert result == []

    def test_returns_all_when_cutoff_older_than_all(self):
        result = entries_newer_than(self._entries(), '2025-01-01')
        assert len(result) == 3

    def test_handles_us_date_via_date_iso(self):
        entries = [
            {'date': '4/29/2026', 'date_iso': '2026-04-29', 'commit': None, 'bullets': ['x']},
        ]
        result = entries_newer_than(entries, '2026-04-28')
        assert len(result) == 1


class TestEntriesInCommits:
    def test_includes_entries_whose_hash_is_in_set(self):
        entries = [
            {'date': '4/29/2026', 'date_iso': '2026-04-29', 'commit': 'aaa1111', 'bullets': ['a']},
            {'date': '4/29/2026', 'date_iso': '2026-04-29', 'commit': 'bbb2222', 'bullets': ['b']},
            {'date': '4/28/2026', 'date_iso': '2026-04-28', 'commit': 'ccc3333', 'bullets': ['c']},
        ]
        result = entries_in_commits(entries, {'aaa1111', 'ccc3333'}, None)
        assert [e['commit'] for e in result] == ['aaa1111', 'ccc3333']

    def test_falls_back_to_date_for_untagged_entries(self):
        entries = [
            {'date': '2026-04-29', 'date_iso': '2026-04-29', 'commit': None, 'bullets': ['a']},
            {'date': '2026-04-21', 'date_iso': '2026-04-21', 'commit': None, 'bullets': ['b']},
        ]
        # Tagged commits set is empty - everything must come from date fallback.
        result = entries_in_commits(entries, set(), '2026-04-25')
        assert [e['date'] for e in result] == ['2026-04-29']

    def test_no_fallback_drops_untagged_when_no_cutoff(self):
        entries = [
            {'date': '2026-04-29', 'date_iso': '2026-04-29', 'commit': None, 'bullets': ['a']},
        ]
        # Without a cutoff date, untagged entries are dropped.
        # This matches the "show me only confirmed-pending stuff" intent.
        result = entries_in_commits(entries, set(), None)
        assert result == []

    def test_mixed_tagged_and_untagged(self):
        entries = [
            {'date': '4/29/2026', 'date_iso': '2026-04-29', 'commit': 'aaa1111', 'bullets': ['a']},
            {'date': '2026-04-28', 'date_iso': '2026-04-28', 'commit': None, 'bullets': ['b']},
            {'date': '4/27/2026', 'date_iso': '2026-04-27', 'commit': 'ccc3333', 'bullets': ['c']},
        ]
        result = entries_in_commits(entries, {'aaa1111'}, '2026-04-27')
        # Tagged hit + untagged date-newer-than-cutoff
        assert [e['date_iso'] for e in result] == ['2026-04-29', '2026-04-28']

    def test_short_hash_match(self):
        # Entry has a 7-char short SHA, commits set has same 7 chars.
        entries = [
            {'date': '4/29/2026', 'date_iso': '2026-04-29', 'commit': 'a3547a9', 'bullets': ['x']},
        ]
        result = entries_in_commits(entries, {'a3547a9'}, None)
        assert len(result) == 1
