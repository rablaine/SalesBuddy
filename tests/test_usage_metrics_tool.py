"""Usage tool delegates to the same App Insights insights as the metrics dashboard."""
from unittest.mock import patch

import pytest

from app.services.salesiq_tools import execute_tool


def test_usage_metrics_tool_reuses_dashboard_query():
    """The tool adds no independent query logic or interactive authentication."""
    payload = {
        'card_id': 'dau', 'rows': [], 'error': 'Sign in required', 'auth_required': True,
    }
    with patch('app.services.usage_metrics.get_metric_card', return_value=payload) as query:
        result = execute_tool('get_usage_metrics', {'insight': 'dau', 'days': 30})
    query.assert_called_once_with('dau', 30)
    assert result == payload


def test_usage_metrics_unknown_insight_is_explicit():
    """An invalid insight cannot silently query a default or return success."""
    with pytest.raises(ValueError, match='Unknown metrics card'):
        execute_tool('get_usage_metrics', {'insight': 'not-an-insight'})
