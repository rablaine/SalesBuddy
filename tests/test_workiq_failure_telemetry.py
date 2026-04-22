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
