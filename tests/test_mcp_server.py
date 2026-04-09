"""Tests for the MCP server module."""
import pytest
import anyio


class TestMCPServerImport:
    """Verify the MCP server module loads and registers tools."""

    def test_mcp_server_imports(self):
        """mcp_server module should import without error."""
        from app import mcp_server
        assert mcp_server.mcp is not None

    def test_mcp_server_has_registered_tools(self):
        """MCP server should register all tools from the shared registry."""
        from app.mcp_server import list_tools
        from app.services.salesiq_tools import TOOLS

        registered = anyio.from_thread.run_sync(list_tools) if False else anyio.run(list_tools)
        assert len(registered) == len(TOOLS), (
            f"MCP registered {len(registered)} tools but registry has {len(TOOLS)}"
        )

    def test_mcp_tool_names_match_registry(self):
        """Every tool in the shared registry should have an MCP counterpart."""
        from app.mcp_server import list_tools
        from app.services.salesiq_tools import TOOLS

        registered = anyio.run(list_tools)
        mcp_names = {t.name for t in registered}
        registry_names = {t['name'] for t in TOOLS}
        assert mcp_names == registry_names

    def test_mcp_tools_have_proper_input_schemas(self):
        """MCP tools should expose the real parameter schemas, not generic kwargs."""
        from app.mcp_server import list_tools

        registered = anyio.run(list_tools)
        for tool in registered:
            schema = tool.inputSchema
            assert schema.get('type') == 'object', f"{tool.name} schema missing type"
            assert 'properties' in schema, f"{tool.name} schema missing properties"
            assert 'kwargs' not in schema.get('properties', {}), (
                f"{tool.name} has generic kwargs instead of real parameters"
            )


class TestMCPToolExecution:
    """Test that MCP tool handlers work within Flask app context."""

    def test_call_tool_returns_text_content(self):
        """call_tool should return a list of TextContent."""
        from app.mcp_server import call_tool

        result = anyio.run(call_tool, 'search_customers', {'query': 'nonexistent_xyz'})
        assert isinstance(result, list)
        assert len(result) > 0
        assert result[0].type == 'text'
        assert isinstance(result[0].text, str)

    def test_call_tool_unknown_raises(self):
        """call_tool should raise ValueError for unknown tools."""
        from app.mcp_server import call_tool

        with pytest.raises(ValueError, match='Unknown tool'):
            anyio.run(call_tool, 'nonexistent_tool', {})

    def test_execute_tool_in_flask_context(self, app):
        """execute_tool should work within a Flask app context."""
        from app.services.salesiq_tools import execute_tool

        with app.app_context():
            result = execute_tool('search_customers', {'query': 'nonexistent_xyz'})
            assert isinstance(result, (list, dict))


class TestMCPServerConfig:
    """Test MCP server configuration."""

    def test_server_name(self):
        """MCP server should be named SalesBuddy."""
        from app.mcp_server import mcp
        assert mcp.name == "SalesBuddy"

    def test_server_has_instructions(self):
        """MCP server should have instructions for the LLM."""
        from app.mcp_server import mcp
        assert mcp.instructions is not None
        assert "Sales Buddy" in mcp.instructions

    def test_flask_app_created(self):
        """MCP server should create its own Flask app for DB context."""
        from app.mcp_server import _flask_app
        assert _flask_app is not None
        assert _flask_app.config is not None


class TestMCPResources:
    """Test MCP resource handlers."""

    def test_list_resources_returns_all(self):
        """list_resources should return all ontology resources."""
        from app.mcp_server import list_resources
        from app.services.salesiq_ontology import RESOURCES

        result = anyio.run(list_resources)
        assert len(result) == len(RESOURCES)
        uris = {str(r.uri) for r in result}
        for r in RESOURCES:
            assert r['uri'] in uris

    def test_read_domain_model(self):
        """read_resource should return domain model content."""
        from app.mcp_server import read_resource

        content = anyio.run(read_resource, 'salesbuddy://domain-model')
        assert isinstance(content, str)
        assert 'Solution Engineer' in content
        assert 'Engagement' in content

    def test_read_glossary(self):
        """read_resource should return glossary content."""
        from app.mcp_server import read_resource

        content = anyio.run(read_resource, 'salesbuddy://glossary')
        assert isinstance(content, str)
        assert 'ACR' in content
        assert 'U2C' in content

    def test_read_workflows(self):
        """read_resource should return workflow content."""
        from app.mcp_server import read_resource

        content = anyio.run(read_resource, 'salesbuddy://workflows')
        assert isinstance(content, str)
        assert 'get_portfolio_overview' in content

    def test_read_unknown_resource_raises(self):
        """read_resource should raise for unknown URI."""
        from app.mcp_server import read_resource

        with pytest.raises(ValueError, match='Unknown resource'):
            anyio.run(read_resource, 'salesbuddy://nonexistent')
