"""Metrics reuse existing sign-ins without opening interactive authentication."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time
from unittest.mock import MagicMock, Mock

import pytest
from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError

from app.services import app_insights


@pytest.fixture(autouse=True)
def reset_auth(monkeypatch):
    """Keep token and failure caches isolated between tests."""
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.side_effect = CredentialUnavailableError('No cached test credentials')
    monkeypatch.setattr(app_insights, 'DefaultAzureCredential', Mock(return_value=credential))
    app_insights.reset_token_cache()
    yield
    app_insights.reset_token_cache()


def test_parallel_cards_acquire_one_token(monkeypatch):
    """Concurrent dashboard cards share one broker-disabled token acquisition."""
    acquire = Mock(return_value=('test-token', time.time() + 3600))
    monkeypatch.setattr(app_insights, '_request_existing_token', acquire)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: app_insights._get_token(), range(16)))
    assert tokens == ['test-token'] * 16
    acquire.assert_called_once_with()


def test_failed_cli_auth_is_shared_without_interactive_fallback(monkeypatch):
    """A signed-out install gets explicit errors, not one auth window per card."""
    acquire = Mock(side_effect=RuntimeError('Sign-in required'))
    monkeypatch.setattr(app_insights, '_request_existing_token', acquire)

    def request_token(_):
        """Model the independent card requests."""
        with pytest.raises(app_insights.AppInsightsError, match='existing credentials') as error:
            app_insights._get_token()
        assert app_insights.is_auth_error(error.value) is False

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(request_token, range(16)))
    assert acquire.call_count == 1
    app_insights.reset_token_cache()
    request_token(None)
    assert acquire.call_count == 2


def test_cli_disables_broker_only_in_child_environment(monkeypatch):
    """The CLI must not inherit WAM even when the user's global broker setting is enabled."""
    monkeypatch.setenv('AZURE_CORE_ENABLE_BROKER_ON_WINDOWS', 'true')
    monkeypatch.setenv('AZURE_CONFIG_DIR', r'C:\test\existing-dev-profile')
    monkeypatch.setattr(app_insights.shutil, 'which', lambda _: r'C:\Azure\az.cmd')
    expiry = int(time.time()) + 3600
    run = Mock(return_value=Mock(stdout=json.dumps({
        'accessToken': 'mock-only-token', 'expires_on': expiry,
    })))
    monkeypatch.setattr(app_insights.subprocess, 'run', run)

    assert app_insights._request_cli_token() == ('mock-only-token', float(expiry))
    command = run.call_args.args[0]
    options = run.call_args.kwargs
    assert command[1:3] == ['account', 'get-access-token']
    assert 'login' not in command
    assert options['env']['AZURE_CORE_ENABLE_BROKER_ON_WINDOWS'] == 'false'
    assert options['env']['AZURE_CORE_ENABLE_BROKER_ON_MAC'] == 'false'
    assert options['env']['AZURE_CONFIG_DIR'] == r'C:\test\existing-dev-profile'
    assert os.environ['AZURE_CORE_ENABLE_BROKER_ON_WINDOWS'] == 'true'
    assert options['stdin'] == subprocess.DEVNULL
    assert options['timeout'] == 10
    assert options['check'] is True


@pytest.mark.parametrize('response', [
    '{}',
    '{"accessToken": "", "expires_on": 9999999999}',
    '{"accessToken": "mock", "expires_on": 0}',
    '{"accessToken": "mock", "expires_on": "NaN"}',
    'not json',
])
def test_invalid_cli_tokens_fail_explicitly(monkeypatch, response):
    """Malformed or expired CLI output cannot be cached as a usable credential."""
    monkeypatch.setattr(app_insights.shutil, 'which', lambda _: r'C:\Azure\az.cmd')
    monkeypatch.setattr(
        app_insights.subprocess, 'run', Mock(return_value=Mock(stdout=response)),
    )
    with pytest.raises(app_insights.AppInsightsError, match='existing credentials'):
        app_insights._get_token()


def test_cli_timeout_does_not_retry_or_start_login(monkeypatch):
    """An unsuccessful passive read never becomes an interactive retry."""
    run = Mock(side_effect=subprocess.TimeoutExpired('az', 10))
    monkeypatch.setattr(app_insights.shutil, 'which', lambda _: r'C:\Azure\az.cmd')
    monkeypatch.setattr(app_insights.subprocess, 'run', run)
    for _ in range(3):
        with pytest.raises(app_insights.AppInsightsError):
            app_insights._get_token()
    assert run.call_count == 2
    assert all('login' not in call.args[0] for call in run.call_args_list)


def test_existing_default_cli_sign_in_survives_missing_resource_tenant(monkeypatch):
    """The original default-tenant fallback works even without a tenant-specific CLI login."""
    acquire = Mock(side_effect=[
        subprocess.CalledProcessError(1, 'az', stderr="Please run 'az login'"),
        ('existing-default-token', time.time() + 3600),
    ])
    monkeypatch.setattr(app_insights, '_request_cli_token', acquire)

    assert app_insights._get_token() == 'existing-default-token'
    assert [call.kwargs for call in acquire.call_args_list] == [
        {'tenant_id': app_insights.TENANT_ID}, {'tenant_id': None},
    ]
    app_insights.DefaultAzureCredential.assert_not_called()


def test_existing_sdk_sign_in_is_preserved_without_automatic_interaction(monkeypatch):
    """Losing the CLI's resource-tenant session must not discard an existing SDK sign-in."""
    monkeypatch.setattr(
        app_insights, '_request_cli_token',
        Mock(side_effect=subprocess.CalledProcessError(1, 'az', stderr="Please run 'az login'")),
    )
    credential = app_insights.DefaultAzureCredential.return_value
    credential.get_token.side_effect = None
    credential.get_token.return_value = AccessToken('existing-sdk-token', int(time.time()) + 3600)

    assert app_insights._get_token() == 'existing-sdk-token'
    app_insights.DefaultAzureCredential.assert_called_once_with(
        additionally_allowed_tenants=[app_insights.TENANT_ID],
        exclude_cli_credential=True,
        exclude_interactive_browser_credential=True,
        disable_automatic_authentication=True,
    )
    credential.get_token.assert_called_once_with(app_insights._TOKEN_SCOPE)
    credential.__exit__.assert_called_once()


def test_default_cli_read_does_not_force_a_different_tenant(monkeypatch):
    """The default fallback preserves the existing CLI profile and selected account."""
    monkeypatch.setattr(app_insights.shutil, 'which', lambda _: r'C:\Azure\az.cmd')
    run = Mock(return_value=Mock(stdout=json.dumps({
        'accessToken': 'existing-token', 'expires_on': int(time.time()) + 3600,
    })))
    monkeypatch.setattr(app_insights.subprocess, 'run', run)
    monkeypatch.setenv('AZURE_CONFIG_DIR', r'C:\test\existing-dev-profile')

    assert app_insights._request_cli_token(tenant_id=None)[0] == 'existing-token'
    assert '--tenant' not in run.call_args.args[0]
    assert run.call_args.kwargs['env']['AZURE_CONFIG_DIR'] == r'C:\test\existing-dev-profile'
    assert run.call_args.kwargs['env']['AZURE_CORE_ENABLE_BROKER_ON_WINDOWS'] == 'false'


def test_auth_timeouts_are_not_reported_as_signed_out(monkeypatch):
    """A timeout plus unavailable fallback credentials does not prove a missing sign-in."""
    acquire = Mock(side_effect=subprocess.TimeoutExpired('az', 10))
    monkeypatch.setattr(app_insights, '_request_cli_token', acquire)
    for _ in range(2):
        with pytest.raises(app_insights.AppInsightsError) as error:
            app_insights._get_token()
        assert app_insights.is_auth_error(error.value) is False
        assert 'No sign-in was started' in str(error.value)
    assert acquire.call_count == 2


def test_explicit_missing_sign_in_remains_actionable(monkeypatch):
    """Only genuine missing-session failures offer interactive sign-in."""
    monkeypatch.setattr(
        app_insights, '_request_cli_token',
        Mock(side_effect=subprocess.CalledProcessError(1, 'az', stderr="Please run 'az login'")),
    )
    for _ in range(2):
        with pytest.raises(app_insights.AppInsightsError) as error:
            app_insights._get_token()
        assert app_insights.is_auth_error(error.value) is True


def test_windows_msi_cli_uses_direct_host_for_enforceable_timeout(monkeypatch):
    """A timeout must terminate the CLI Python process, not leave it behind az.cmd."""
    monkeypatch.setattr(
        app_insights.shutil, 'which', lambda _: r'C:\Azure\CLI2\wbin\az.cmd',
    )
    monkeypatch.setattr(app_insights.Path, 'is_file', lambda _: True)
    run = Mock(return_value=Mock(stdout=json.dumps({
        'accessToken': 'mock-only-token', 'expires_on': int(time.time()) + 3600,
    })))
    monkeypatch.setattr(app_insights.subprocess, 'run', run)
    app_insights._request_cli_token()
    assert run.call_args.args[0][:3] == [
        r'C:\Azure\CLI2\python.exe', '-IBm', 'azure.cli',
    ]


def test_partial_query_error_is_not_reported_as_success(monkeypatch):
    """A 200 containing partial results and an error must remain a failed card."""
    monkeypatch.setattr(app_insights, '_get_token', lambda: 'mock-only-token')
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        'error': {'code': 'PartialError', 'message': 'Query execution was incomplete'},
        'tables': [{'columns': [{'name': 'count', 'type': 'long'}], 'rows': [[1]]}],
    }
    monkeypatch.setattr(app_insights, '_get_session', lambda: session)
    with pytest.raises(app_insights.AppInsightsError, match='incomplete'):
        app_insights.query('mock query')
