"""
Tests for WorkIQ call telemetry.

Verifies that ``queue_workiq_call`` is invoked with the correct
operation/status/failure_type taxonomy from each call path in
``app.services.workiq_service`` and the parser sites, and that the
helper itself respects the opt-out flag and clamps to the allowed
taxonomy.
"""
import subprocess
from unittest.mock import patch, MagicMock

import pytest


# =============================================================================
# queue_workiq_call helper
# =============================================================================

class TestQueueWorkiqCall:
    """Unit tests for the telemetry shipper helper."""

    def test_emits_envelope_when_enabled(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call(
                'meeting_summary', 'server_down',
                failure_type='subprocess_timeout', duration_ms=12345.6,
            )
            assert len(buf) == 1
            base = buf[0]['data']['baseData']
            assert base['name'] == 'SalesBuddy.WorkIQCall'
            assert base['properties']['operation'] == 'meeting_summary'
            assert base['properties']['status'] == 'server_down'
            assert base['properties']['failure_type'] == 'subprocess_timeout'
            assert base['measurements']['count'] == 1.0
            assert base['measurements']['duration_ms'] == 12345.6

    def test_ok_status_drops_failure_type(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call(
                'attendee_scrape', 'ok', duration_ms=420.0,
            )
            base = buf[0]['data']['baseData']
            assert base['properties']['status'] == 'ok'
            assert 'failure_type' not in base['properties']
            assert base['measurements']['duration_ms'] == 420.0

    def test_no_op_when_opted_out(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=False), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call('query', 'server_down',
                                                failure_type='nonzero_exit')
            assert buf == []

    def test_unknown_operation_is_normalized(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call('definitely-not-real',
                                                'server_down',
                                                failure_type='server_error')
            assert buf[0]['data']['baseData']['properties']['operation'] == 'query'

    def test_unknown_status_is_normalized_to_server_down(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call('query', 'totally-bogus',
                                                failure_type='nonzero_exit')
            assert buf[0]['data']['baseData']['properties']['status'] == 'server_down'

    def test_unknown_failure_type_is_normalized(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call('query', 'server_down',
                                                failure_type='free-form-error-text')
            base = buf[0]['data']['baseData']
            assert base['properties']['failure_type'] == 'nonzero_exit'

    def test_duration_omitted_when_none(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call('query', 'server_down',
                                                failure_type='npx_missing')
            assert 'duration_ms' not in buf[0]['data']['baseData']['measurements']

    def test_calendar_lookup_failure_is_preserved(self):
        from app.services import telemetry_shipper

        with patch.object(telemetry_shipper, 'is_telemetry_enabled', return_value=True), \
             patch.object(telemetry_shipper, '_buffer', []) as buf:
            telemetry_shipper.queue_workiq_call(
                'meeting_list',
                'server_down',
                failure_type='calendar_lookup_failed',
            )

            properties = buf[0]['data']['baseData']['properties']
            assert properties['failure_type'] == 'calendar_lookup_failed'


# =============================================================================
# query_workiq failure path coverage
# =============================================================================

class TestQueryWorkiqFailures:
    """Each failure path emits a server_down event via _record_workiq_failure."""

    def test_npx_missing_records_failure(self):
        from app.services import workiq_service

        with patch('shutil.which', return_value=None), \
             patch('platform.system', return_value='Linux'), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(RuntimeError, match='npx not found'):
                workiq_service.query_workiq('hi', operation='meeting_list')
            rec.assert_called_once()
            args, _ = rec.call_args
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

    def test_none_stdout_returns_clear_empty_response_error(self):
        """Successful subprocesses with no stdout never leak AttributeError."""
        from app.services import workiq_service

        fake_result = MagicMock(returncode=0, stderr=None, stdout=None)
        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run', return_value=fake_result), \
             patch.object(workiq_service, '_record_workiq_failure') as rec:
            with pytest.raises(RuntimeError, match='empty response'):
                workiq_service.query_workiq('hi', operation='meeting_list')

            rec.assert_called_once()
            assert rec.call_args.args[0] == 'meeting_list'
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
# query_workiq success path
# =============================================================================

class TestQueryWorkiqSuccess:
    """A successful query emits an ok call event so uptime % is computable."""

    def test_success_records_ok_call(self):
        from app.services import workiq_service

        fake_result = MagicMock(returncode=0, stderr='', stdout='clean response')
        with patch('shutil.which', return_value='/usr/bin/npx'), \
             patch('platform.system', return_value='Linux'), \
             patch('subprocess.run', return_value=fake_result), \
             patch.object(workiq_service, '_record_workiq_call') as rec:
            workiq_service.query_workiq('hi', operation='meeting_summary')
            ok_calls = [c for c in rec.call_args_list
                        if len(c.args) >= 2 and c.args[1] == 'ok']
            assert len(ok_calls) == 1
            assert ok_calls[0].args[0] == 'meeting_summary'


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
# Parser site failure coverage
# =============================================================================

class TestAttendeeScrapeFailures:
    """Verifies attendee scrape emits parse_failed when WorkIQ returns prose."""

    def test_non_json_response_records_failure(self):
        from app.services import meeting_attendee_scrape

        prose = "I don't have a transcript for that meeting yet, sorry."
        with patch('app.services.telemetry_shipper.queue_workiq_call') as q:
            result = meeting_attendee_scrape._parse_response(prose)
            assert result == []
            q.assert_called_once_with(
                'attendee_scrape', 'parse_failed',
                failure_type='parse_attendee_json',
            )

    def test_valid_json_does_not_record_failure(self):
        from app.services import meeting_attendee_scrape

        valid = '{"attendees": [{"name": "X", "email": "x@y.com", "title": null}]}'
        with patch('app.services.telemetry_shipper.queue_workiq_call') as q:
            result = meeting_attendee_scrape._parse_response(valid)
            assert len(result) == 1
            q.assert_not_called()


class TestCustomerScrapeParseFailure:
    def test_non_json_response_records_parse_failed(self):
        from app.services import customer_scrape

        with patch('app.services.telemetry_shipper.queue_workiq_call') as q:
            result = customer_scrape._parse_response('no json here at all')
            assert result == {'contacts': [], 'meetings_found': 0}
            q.assert_called_once_with(
                'customer_scrape', 'parse_failed',
                failure_type='parse_customer_json',
            )


class TestPartnerScrapeParseFailure:
    def test_non_json_response_records_parse_failed(self):
        from app.services import partner_scrape

        with patch('app.services.telemetry_shipper.queue_workiq_call') as q:
            result = partner_scrape._parse_response('still no json')
            assert result['contacts'] == []
            q.assert_called_once_with(
                'partner_scrape', 'parse_failed',
                failure_type='parse_partner_json',
            )


class TestCopilotActionsParseFailure:
    def test_non_json_response_records_parse_failed(self):
        from app.services import copilot_actions

        with patch('app.services.telemetry_shipper.queue_workiq_call') as q:
            result = copilot_actions.parse_action_items('definitely no array here')
            assert result == []
            q.assert_called_once_with(
                'copilot_actions', 'parse_failed',
                failure_type='parse_copilot_actions_json',
            )


# =============================================================================
# ANSI escape stripping in query_workiq
# =============================================================================

class TestAnsiStripping:
    """WorkIQ wraps every line in ``\\x1b[90m...\\x1b[0m`` on Windows.

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
        assert _ANSI_ESCAPE_RE.sub('', 'array [90m]') == 'array [90m]'
