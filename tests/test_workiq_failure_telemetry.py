"""
Tests for WorkIQ failure telemetry.

Verifies that ``queue_workiq_failure`` is called with the correct
operation/failure_type taxonomy from each failure path in
``app.services.workiq_service``, and that the helper itself respects
the opt-out flag and the allowed taxonomy.
"""
import subprocess
from unittest.mock import patch, MagicMock

import pytest


# =============================================================================
# queue_workiq_failure helper
# =============================================================================

class TestQueueWorkiqFailure:
    """Unit tests for the telemetry shipper helper."""

    def test_emits_envelope_when_enabled(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_failure(
                'meeting_summary', 'subprocess_timeout', duration_ms=12345.6
            )
            assert len(buf) == 1
            envelope = buf[0]
            base = envelope['data']['baseData']
            assert base['name'] == 'SalesBuddy.WorkIQFailure'
            assert base['properties']['operation'] == 'meeting_summary'
            assert base['properties']['failure_type'] == 'subprocess_timeout'
            assert base['measurements']['count'] == 1.0
            assert base['measurements']['duration_ms'] == 12345.6

    def test_no_op_when_opted_out(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=False), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_failure('query', 'nonzero_exit')
            assert buf == []

    def test_unknown_operation_is_normalized(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_failure('definitely-not-real', 'server_error')
            assert buf[0]['data']['baseData']['properties']['operation'] == 'query'

    def test_unknown_failure_type_is_normalized(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_failure('query', 'free-form-error-text')
            assert buf[0]['data']['baseData']['properties']['failure_type'] == 'nonzero_exit'

    def test_duration_omitted_when_none(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_failure('query', 'npx_missing')
            assert 'duration_ms' not in buf[0]['data']['baseData']['measurements']


# =============================================================================
# query_workiq failure path coverage
# =============================================================================

class TestQueryWorkiqFailures:
    """Verifies query_workiq emits failure telemetry for each failure path."""

    def test_npx_missing_records_failure(self):
        from app.services import workiq_service

        with patch('shutil.which', return_value=None), \
             patch('platform.system', return_value='Linux'), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(RuntimeError, match='npx not found'):
                workiq_service.query_workiq('hi', operation='meeting_list')
            rec.assert_called_once()
            args, kwargs = rec.call_args
            assert args[0] == 'meeting_list'
            assert args[1] == 'npx_missing'

    def test_nonzero_exit_records_failure(self):
        from app.services import workiq_service

        fake_result = MagicMock(returncode=1, stderr='boom', stdout='')
        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run', return_value=fake_result), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(RuntimeError, match='WorkIQ query failed'):
                workiq_service.query_workiq('hi', operation='query')
            rec.assert_called_once()
            assert rec.call_args.args[1] == 'nonzero_exit'

    def test_server_error_in_stdout_records_failure(self):
        from app.services import workiq_service

        fake_result = MagicMock(
            returncode=0,
            stderr='',
            stdout='Error: Server error: backend exploded',
        )
        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run', return_value=fake_result), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(RuntimeError, match='server issues'):
                workiq_service.query_workiq('hi', operation='meeting_summary')
            rec.assert_called_once()
            assert rec.call_args.args[0] == 'meeting_summary'
            assert rec.call_args.args[1] == 'server_error'

    def test_subprocess_timeout_records_failure(self):
        from app.services import workiq_service

        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='npx', timeout=1)), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(TimeoutError):
                workiq_service.query_workiq('hi', timeout=1, operation='meeting_summary')
            rec.assert_called_once()
            assert rec.call_args.args[1] == 'subprocess_timeout'


# =============================================================================
# get_meeting_summary soft-failure coverage
# =============================================================================

class TestMeetingSummarySoftFailures:
    """Soft-failures (planning narration, refusal, too-short) emit telemetry."""

    def test_refusal_records_failure(self):
        from app.services import workiq_service

        refusal = "Sorry, I can't help with that. Let's talk about something else."
        with patch.object(workiq_service, 'query_workiq', return_value=refusal), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            result = workiq_service.get_meeting_summary('Some Meeting', '2026-04-22')
            assert result['summary'] == ''
            assert result.get('retry_suggested') is True
            assert any(c.args == ('meeting_summary', 'refusal')
                       for c in rec.call_args_list)

    def test_too_short_records_failure(self):
        from app.services import workiq_service

        with patch.object(workiq_service, 'query_workiq', return_value='ok.'), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            result = workiq_service.get_meeting_summary('Some Meeting', '2026-04-22')
            assert result['summary'] == ''
            assert any(c.args == ('meeting_summary', 'too_short')
                       for c in rec.call_args_list)

    def test_planning_narration_records_failure(self):
        from app.services import workiq_service

        # Two planning phrases trigger the planning_narration branch.
        narration = (
            "I need to retrieve the meeting data first. "
            "I'm going to search your Microsoft 365 mailbox to ground the summary in "
            "the actual content. Once I have that I can write the summary."
        )
        with patch.object(workiq_service, 'query_workiq', return_value=narration), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            result = workiq_service.get_meeting_summary('Some Meeting', '2026-04-22')
            assert result.get('retry_suggested') is True
            assert any(c.args == ('meeting_summary', 'planning_narration')
                       for c in rec.call_args_list)


# =============================================================================
# Attendee scrape JSON parse failure
# =============================================================================

class TestAttendeeScrapeFailures:
    """Verifies attendee scrape emits json_parse_failed when WorkIQ returns prose."""

    def test_non_json_response_records_failure(self):
        from app.services import meeting_attendee_scrape

        # Prose response with no JSON braces - common when meeting has no transcript.
        prose = "I don't have a transcript for that meeting yet, sorry."
        with patch('app.services.telemetry_shipper.queue_workiq_failure') as q:
            result = meeting_attendee_scrape._parse_response(prose)
            assert result == []
            q.assert_called_once_with('attendee_scrape', 'json_parse_failed')

    def test_valid_json_does_not_record_failure(self):
        from app.services import meeting_attendee_scrape

        valid = '{"attendees": [{"name": "X", "email": "x@y.com", "title": null}]}'
        with patch('app.services.telemetry_shipper.queue_workiq_failure') as q:
            result = meeting_attendee_scrape._parse_response(valid)
            assert len(result) == 1
            q.assert_not_called()


# =============================================================================
# ANSI escape stripping in query_workiq
# =============================================================================

class TestAnsiStripping:
    """WorkIQ wraps every line in `\\x1b[90m...\\x1b[0m` on Windows.

    These escapes are invisible in the terminal but break every JSON parser
    downstream. ``query_workiq`` strips them at the boundary so callers see
    clean text.
    """

    def test_ansi_escapes_stripped_from_response(self):
        from app.services import workiq_service

        ansi_wrapped = (
            '\x1b[90m\x1b[90m{\n'
            '\x1b[90m  "attendees": [\n'
            '\x1b[90m    {"name": "X", "email": "x@y.com"}\n'
            '\x1b[90m  ]\n'
            '\x1b[90m}\n'
            '\x1b[0m'
        )
        fake_result = MagicMock(returncode=0, stderr='', stdout=ansi_wrapped)
        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run', return_value=fake_result):
            cleaned = workiq_service.query_workiq('hi', operation='attendee_scrape')
            assert '\x1b' not in cleaned
            assert '[90m' not in cleaned
            assert '"attendees"' in cleaned

    def test_ansi_strip_regex_matches_common_codes(self):
        from app.services.workiq_service import _ANSI_ESCAPE_RE

        assert _ANSI_ESCAPE_RE.sub('', '\x1b[90mhi\x1b[0m') == 'hi'
        assert _ANSI_ESCAPE_RE.sub('', '\x1b[1;31mred\x1b[0m') == 'red'
        # No false positives on literal `[90m` text without ESC prefix.
        assert _ANSI_ESCAPE_RE.sub('', 'array [90m]') == 'array [90m]'
