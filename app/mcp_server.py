"""MCP server for Sales Buddy.

Exposes the SalesIQ tool registry to VS Code Copilot via the
Model Context Protocol. All tools are read-only queries against
the local SQLite database.

Usage (stdio transport):
    python -m app.mcp_server
"""
import json
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, Resource
import anyio

from app import create_app
from app.services.salesiq_tools import TOOLS, execute_tool
from app.services.salesiq_ontology import RESOURCES

# Flask app context needed for DB access
_flask_app = create_app()

mcp = Server(
    "SalesBuddy",
    instructions=(
        "Sales Buddy is a note-taking app for Azure technical sellers. "
        "Use these tools to query customers, notes, engagements, milestones, "
        "sellers, opportunities, partners, action items, and reports."
    ),
)


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    """Return all SalesIQ tools with their proper JSON Schema parameters."""
    return [
        Tool(
            name=t['name'],
            description=t['description'],
            inputSchema=t['parameters'],
        )
        for t in TOOLS
    ]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a tool by name within a Flask app context."""
    with _flask_app.app_context():
        result = execute_tool(name, arguments)
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, default=str)
    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Resources - domain ontology for LLM context
# ---------------------------------------------------------------------------

@mcp.list_resources()
async def list_resources() -> list[Resource]:
    """Return available domain knowledge resources."""
    return [
        Resource(
            uri=r['uri'],
            name=r['name'],
            description=r['description'],
            mimeType="text/plain",
        )
        for r in RESOURCES
    ]


@mcp.read_resource()
async def read_resource(uri: str) -> str:
    """Return the content of a domain knowledge resource."""
    uri_str = str(uri)
    for r in RESOURCES:
        if r['uri'] == uri_str:
            return r['content']
    raise ValueError(f'Unknown resource: {uri}')


async def _run():
    async with stdio_server() as (read, write):
        await mcp.run(read, write, mcp.create_initialization_options())


if __name__ == "__main__":
    anyio.run(_run)
