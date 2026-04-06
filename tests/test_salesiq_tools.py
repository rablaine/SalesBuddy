"""Tests for the Copilot tool registry."""
import pytest
from app.services.salesiq_tools import TOOLS, get_openai_tools, get_mcp_tools, execute_tool


class TestToolRegistry:
    """Basic registry mechanics."""

    def test_tools_registered(self):
        """At least one tool should be registered."""
        assert len(TOOLS) > 0

    def test_all_tools_have_required_fields(self):
        """Every registered tool must have name, description, parameters, handler."""
        for t in TOOLS:
            assert 'name' in t, f"Tool missing 'name': {t}"
            assert 'description' in t, f"Tool {t.get('name')} missing 'description'"
            assert 'parameters' in t, f"Tool {t['name']} missing 'parameters'"
            assert 'handler' in t, f"Tool {t['name']} missing 'handler'"
            assert callable(t['handler']), f"Tool {t['name']} handler not callable"

    def test_unique_tool_names(self):
        """No duplicate tool names."""
        names = [t['name'] for t in TOOLS]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"

    def test_parameters_are_valid_json_schema(self):
        """Each tool's parameters should be a JSON Schema object."""
        for t in TOOLS:
            params = t['parameters']
            assert params.get('type') == 'object', (
                f"Tool {t['name']} parameters must be type 'object'"
            )
            assert 'properties' in params, (
                f"Tool {t['name']} parameters must have 'properties'"
            )

    def test_get_openai_tools_format(self):
        """get_openai_tools should return OpenAI function calling format."""
        tools = get_openai_tools()
        assert len(tools) == len(TOOLS)
        for t in tools:
            assert t['type'] == 'function'
            assert 'name' in t['function']
            assert 'description' in t['function']
            assert 'parameters' in t['function']

    def test_get_mcp_tools_format(self):
        """get_mcp_tools should return MCP tool format."""
        tools = get_mcp_tools()
        assert len(tools) == len(TOOLS)
        for t in tools:
            assert 'name' in t
            assert 'description' in t
            assert 'inputSchema' in t

    def test_execute_unknown_tool_raises(self):
        """execute_tool should raise ValueError for unknown tools."""
        with pytest.raises(ValueError, match='Unknown tool'):
            execute_tool('nonexistent_tool', {})


class TestToolCoverage:
    """Ensure every core entity and report has at least one registered tool.

    If you add a new model or report, add its keyword here. This test
    enforces the copilot-instructions rule about keeping tools up to date.
    """

    def _tool_names(self) -> set[str]:
        return {t['name'] for t in TOOLS}

    # -- Core entities -------------------------------------------------------

    def test_customer_tool_exists(self):
        """Customers should have a search or summary tool."""
        names = self._tool_names()
        assert any('customer' in n for n in names), (
            'No tool covers customers. Add one to salesiq_tools.py.'
        )

    def test_note_tool_exists(self):
        """Notes should have a search tool."""
        names = self._tool_names()
        assert any('note' in n for n in names), (
            'No tool covers notes. Add one to salesiq_tools.py.'
        )

    def test_engagement_tool_exists(self):
        """Engagements should have a details tool."""
        names = self._tool_names()
        assert any('engagement' in n for n in names), (
            'No tool covers engagements. Add one to salesiq_tools.py.'
        )

    def test_milestone_tool_exists(self):
        """Milestones should have a status/search tool."""
        names = self._tool_names()
        assert any('milestone' in n for n in names), (
            'No tool covers milestones. Add one to salesiq_tools.py.'
        )

    def test_seller_tool_exists(self):
        """Sellers should have a workload tool."""
        names = self._tool_names()
        assert any('seller' in n for n in names), (
            'No tool covers sellers. Add one to salesiq_tools.py.'
        )

    def test_opportunity_tool_exists(self):
        """Opportunities should have a details tool."""
        names = self._tool_names()
        assert any('opportunity' in n for n in names), (
            'No tool covers opportunities. Add one to salesiq_tools.py.'
        )

    def test_partner_tool_exists(self):
        """Partners should have a search tool."""
        names = self._tool_names()
        assert any('partner' in n for n in names), (
            'No tool covers partners. Add one to salesiq_tools.py.'
        )

    def test_action_item_tool_exists(self):
        """Action items should have a list tool."""
        names = self._tool_names()
        assert any('action' in n for n in names), (
            'No tool covers action items. Add one to salesiq_tools.py.'
        )

    # -- Reports -------------------------------------------------------------

    def test_hygiene_report_tool_exists(self):
        """Hygiene report should have a tool."""
        names = self._tool_names()
        assert any('hygiene' in n for n in names), (
            'No tool covers the hygiene report. Add one to salesiq_tools.py.'
        )

    def test_workload_report_tool_exists(self):
        """Workload report should have a tool."""
        names = self._tool_names()
        assert any('workload' in n for n in names), (
            'No tool covers the workload report. Add one to salesiq_tools.py.'
        )

    def test_whats_new_report_tool_exists(self):
        """What's New report should have a tool."""
        names = self._tool_names()
        assert any('whats_new' in n or 'new' in n for n in names), (
            "No tool covers the what's new report. Add one to salesiq_tools.py."
        )

    def test_revenue_alerts_tool_exists(self):
        """Revenue alerts should have a tool."""
        names = self._tool_names()
        assert any('revenue' in n for n in names), (
            'No tool covers revenue alerts. Add one to salesiq_tools.py.'
        )

    def test_whitespace_report_tool_exists(self):
        """Whitespace report should have a tool."""
        names = self._tool_names()
        assert any('whitespace' in n for n in names), (
            'No tool covers the whitespace report. Add one to salesiq_tools.py.'
        )

    # -- Phase 4 entities/reports -------------------------------------------

    def test_territory_tool_exists(self):
        """Territories should have a summary tool."""
        names = self._tool_names()
        assert any('territory' in n for n in names), (
            'No tool covers territories. Add one to salesiq_tools.py.'
        )

    def test_pod_tool_exists(self):
        """PODs should have an overview tool."""
        names = self._tool_names()
        assert any('pod' in n for n in names), (
            'No tool covers PODs. Add one to salesiq_tools.py.'
        )

    def test_analytics_tool_exists(self):
        """Analytics summary should have a tool."""
        names = self._tool_names()
        assert any('analytics' in n for n in names), (
            'No tool covers analytics. Add one to salesiq_tools.py.'
        )

    def test_one_on_one_tool_exists(self):
        """1:1 report should have a tool."""
        names = self._tool_names()
        assert any('one_on_one' in n for n in names), (
            'No tool covers 1:1 report. Add one to salesiq_tools.py.'
        )

    def test_contact_tool_exists(self):
        """Contacts should have a search tool."""
        names = self._tool_names()
        assert any('contact' in n for n in names), (
            'No tool covers contacts. Add one to salesiq_tools.py.'
        )

    def test_milestones_due_soon_tool_exists(self):
        """Milestones due soon should have a tool."""
        names = self._tool_names()
        assert any('due' in n for n in names), (
            'No tool covers milestones due soon. Add one to salesiq_tools.py.'
        )

    def test_marketing_insights_tool_exists(self):
        """Marketing insights should have a tool."""
        names = self._tool_names()
        assert any('marketing' in n for n in names), (
            'No tool covers marketing insights. Add one to salesiq_tools.py.'
        )


class TestToolExecution:
    """Test that tools actually execute against the DB without errors."""

    def test_search_customers_empty(self, app):
        """search_customers with no data should return empty list."""
        with app.app_context():
            result = execute_tool('search_customers', {'query': 'nonexistent'})
            assert isinstance(result, list)
            assert len(result) == 0

    def test_search_customers_finds_match(self, app, sample_data):
        """search_customers should find matching customers."""
        with app.app_context():
            result = execute_tool('search_customers', {'query': 'Acme'})
            assert len(result) >= 1
            assert result[0]['name'] == 'Acme Corp'

    def test_get_customer_summary(self, app, sample_data):
        """get_customer_summary should return structured data."""
        with app.app_context():
            cid = sample_data['customer1_id']
            result = execute_tool('get_customer_summary', {'customer_id': cid})
            assert result['name'] == 'Acme Corp'
            assert 'engagements' in result
            assert 'milestones' in result
            assert 'recent_notes' in result

    def test_get_customer_summary_not_found(self, app):
        """get_customer_summary with bad ID should return error."""
        with app.app_context():
            result = execute_tool('get_customer_summary', {'customer_id': 99999})
            assert 'error' in result

    def test_search_notes_empty(self, app):
        """search_notes with no matches should return empty list."""
        with app.app_context():
            result = execute_tool('search_notes', {'query': 'zzz_no_match'})
            assert isinstance(result, list)

    def test_search_notes_finds_match(self, app, sample_data):
        """search_notes should find notes by keyword."""
        with app.app_context():
            result = execute_tool('search_notes', {'query': 'migration'})
            assert len(result) >= 1

    def test_get_engagement_details_not_found(self, app):
        """get_engagement_details with bad ID should return error."""
        with app.app_context():
            result = execute_tool('get_engagement_details', {'engagement_id': 99999})
            assert 'error' in result

    def test_get_milestone_status_list(self, app):
        """get_milestone_status without ID should return list."""
        with app.app_context():
            result = execute_tool('get_milestone_status', {})
            assert isinstance(result, list)

    def test_get_seller_workload(self, app, sample_data):
        """get_seller_workload should return seller stats."""
        with app.app_context():
            result = execute_tool('get_seller_workload', {
                'seller_id': sample_data['seller1_id']
            })
            assert result['name'] == 'Alice Smith'
            assert 'customer_count' in result

    def test_get_seller_workload_not_found(self, app):
        """get_seller_workload with bad ID should return error."""
        with app.app_context():
            result = execute_tool('get_seller_workload', {'seller_id': 99999})
            assert 'error' in result

    def test_search_partners_empty(self, app):
        """search_partners with no data should return empty list."""
        with app.app_context():
            result = execute_tool('search_partners', {'query': 'nonexistent'})
            assert isinstance(result, list)

    def test_list_action_items_empty(self, app):
        """list_action_items with no data should return empty list."""
        with app.app_context():
            result = execute_tool('list_action_items', {})
            assert isinstance(result, list)

    def test_report_hygiene(self, app):
        """report_hygiene should return structured data."""
        with app.app_context():
            result = execute_tool('report_hygiene', {})
            assert 'engagements_without_milestones' in result
            assert 'milestones_without_engagements' in result

    def test_report_workload(self, app):
        """report_workload should return structured data."""
        with app.app_context():
            result = execute_tool('report_workload', {})
            assert 'customer_count' in result
            assert 'customers' in result

    def test_report_whats_new(self, app):
        """report_whats_new should return created/updated lists."""
        with app.app_context():
            result = execute_tool('report_whats_new', {'days': 7})
            assert 'created' in result
            assert 'updated' in result

    def test_report_revenue_alerts(self, app):
        """report_revenue_alerts should return list."""
        with app.app_context():
            result = execute_tool('report_revenue_alerts', {})
            assert isinstance(result, list)

    def test_report_whitespace_needs_params(self, app):
        """report_whitespace without params should return error guidance."""
        with app.app_context():
            result = execute_tool('report_whitespace', {})
            assert 'error' in result

    # -- Phase 4 tool execution tests ---------------------------------------

    def test_get_milestones_due_soon_empty(self, app):
        """get_milestones_due_soon with no data returns empty list."""
        with app.app_context():
            result = execute_tool('get_milestones_due_soon', {})
            assert isinstance(result, list)

    def test_get_territory_summary_not_found(self, app):
        """get_territory_summary with bad ID returns error."""
        with app.app_context():
            result = execute_tool('get_territory_summary', {'territory_id': 99999})
            assert 'error' in result

    def test_get_territory_summary(self, app, sample_data):
        """get_territory_summary returns structured data."""
        with app.app_context():
            from app.models import db, Territory
            t = Territory(name='West')
            db.session.add(t)
            db.session.commit()
            result = execute_tool('get_territory_summary', {'territory_id': t.id})
            assert result['name'] == 'West'
            assert 'customers' in result
            assert 'sellers' in result

    def test_get_pod_overview_not_found(self, app):
        """get_pod_overview with bad ID returns error."""
        with app.app_context():
            result = execute_tool('get_pod_overview', {'pod_id': 99999})
            assert 'error' in result

    def test_get_pod_overview(self, app, sample_data):
        """get_pod_overview returns structured data."""
        with app.app_context():
            from app.models import db, POD
            p = POD(name='Alpha Pod')
            db.session.add(p)
            db.session.commit()
            result = execute_tool('get_pod_overview', {'pod_id': p.id})
            assert result['name'] == 'Alpha Pod'
            assert 'territories' in result
            assert 'solution_engineers' in result

    def test_get_analytics_summary(self, app):
        """get_analytics_summary returns structured data."""
        with app.app_context():
            result = execute_tool('get_analytics_summary', {'days': 30})
            assert 'total_notes' in result
            assert 'active_customers' in result
            assert 'top_topics' in result

    def test_report_one_on_one(self, app):
        """report_one_on_one returns structured data."""
        with app.app_context():
            result = execute_tool('report_one_on_one', {'days': 14})
            assert 'customer_activity' in result
            assert 'open_engagements' in result

    def test_search_contacts_empty(self, app):
        """search_contacts with no data returns empty list."""
        with app.app_context():
            result = execute_tool('search_contacts', {'query': 'nonexistent'})
            assert isinstance(result, list)
            assert len(result) == 0

    def test_get_revenue_customer_detail_needs_params(self, app):
        """get_revenue_customer_detail without params returns error."""
        with app.app_context():
            result = execute_tool('get_revenue_customer_detail', {})
            assert 'error' in result
