# Copilot Agent & MCP Server

## Overview

Build an in-app AI chat panel ("Copilot") backed by Azure OpenAI via the existing APIM gateway, plus an MCP server so VS Code Copilot can interact with SalesBuddy data. Both consume a shared tool registry so logic is never duplicated.

## Architecture

```
Browser (Chat Panel)          VS Code (MCP Client)
        |                              |
        | POST /api/ai/chat            | stdio/SSE
        v                              v
  Flask Backend               MCP Server (app/mcp_server.py)
        |                              |
        +--------- Shared Tool Registry --------+
                  app/services/copilot_tools.py
                         |
                   Existing service layer
                   (models, queries, routes)
```

## Key Files

- `app/services/copilot_tools.py` - Tool definitions + handler functions (shared)
- `app/routes/ai.py` - `POST /api/ai/chat` endpoint (chat panel backend)
- `app/mcp_server.py` - MCP server for VS Code/external clients
- `templates/partials/_chat_panel.html` - Chat panel UI partial
- `static/js/chat-panel.js` - Chat panel client-side logic (optional, could be inline)

## Phase 1: Shared Tool Registry

Create `app/services/copilot_tools.py` with:

- Tool definition format: name, description, parameter schema (OpenAI-compatible JSON Schema), handler function reference
- Handler functions that call existing service layer code (no new DB queries - reuse what we have)
- A `get_openai_tools()` helper that converts definitions to OpenAI function calling format
- A `get_mcp_tools()` helper that converts definitions to MCP tool format
- An `execute_tool(name, params, context)` dispatcher that invokes the right handler

### Starter Tools (Read-Only)

| Tool | Description | Uses |
|------|-------------|------|
| `search_notes` | Search call notes by keyword, customer, date range, seller, topic | Existing notes query logic |
| `get_customer_summary` | Activity summary for a customer (engagements, recent notes, milestones, contacts) | Customer view data |
| `get_milestone_status` | Get milestone details or list milestones by status/customer/seller | Milestone tracker data |
| `get_engagement_details` | Get engagement info including action items and linked notes | Engagement view data |
| `list_customers` | Search/list customers by name, territory, seller | Customer list query |
| `get_seller_workload` | Seller's customers, open engagements, upcoming milestones | Seller view data |

### Later Tools (Write - Require Confirmation)

| Tool | Description | Confirmation Required |
|------|-------------|----------------------|
| `update_engagement_status` | Change engagement status (Active/Won/Lost/On Hold) | Yes - show before/after |
| `add_note_comment` | Add a comment to a note | Yes - show preview |
| `create_note` | Create a new call note | Yes - show draft |
| `update_milestone_status` | Change milestone status | Yes - show before/after |
| `add_milestone_comment` | Add comment to a milestone | Yes - show preview |

## Phase 2: Chat Panel UI

### Panel Design

- Collapsible side panel, pinned to right edge (like the existing flyouts but wider, ~450px)
- Toggle button in the navbar (brain or sparkle icon)
- Message history with user/assistant bubbles
- Markdown rendering in assistant messages (links, tables, lists, code)
- Typing indicator while waiting for response
- Page context shown as a subtle chip at the top ("Talking about: Contoso")

### Page Context System

Each template emits a `window.copilotContext` object:

```js
// customer_view.html
window.copilotContext = {
    page: 'customer_view',
    customer_id: 42,
    customer_name: 'Contoso',
    summary: 'Customer with 3 active engagements, 5 milestones'
};
```

The chat panel sends this with each message so the AI knows what page the user is on without asking.

### Chat Flow

1. User types message
2. JS sends POST to `/api/ai/chat` with: message, conversation history, page context
3. Backend builds system prompt (persona + page context + available tools)
4. Backend calls APIM gateway with tool definitions
5. If AI returns tool_calls, backend executes them and sends results back to AI
6. AI formulates final response
7. Backend returns response (and any side effects for write actions)
8. Panel renders response with markdown
9. For write actions: panel shows confirmation card before executing

### Conversation Persistence

- Session-only (localStorage) - conversations don't survive page refreshes initially
- Later: optional server-side storage if users want history

## Phase 3: Chat Backend

### Endpoint: POST /api/ai/chat

Request:
```json
{
    "message": "What milestones are at risk for Contoso?",
    "history": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
    ],
    "context": {
        "page": "customer_view",
        "customer_id": 42
    }
}
```

Response:
```json
{
    "reply": "Contoso has 2 at-risk milestones:\n- **Fabric POC** - due Apr 15...",
    "actions": [],
    "tools_used": ["get_milestone_status"]
}
```

For write actions, response includes an `actions` array with pending changes that need confirmation:
```json
{
    "reply": "I'll mark the Fabric POC engagement as Won. Here's what will change:",
    "actions": [
        {
            "id": "act_1",
            "tool": "update_engagement_status",
            "params": {"engagement_id": 7, "status": "Won"},
            "preview": "Fabric POC: Active -> Won"
        }
    ]
}
```

Then a follow-up `POST /api/ai/chat/confirm` to execute.

### System Prompt

Build dynamically per request:
- Base persona: "You are a helpful assistant for Azure technical sellers using Sales Buddy..."
- Page context: "The user is currently viewing customer Contoso (TPID 12345)..."
- Available tools description
- Behavioral rules: "For write actions, always describe what you'll change and wait for confirmation"

### Gateway Integration

Uses existing `gateway_client.py` pattern. Add a new gateway endpoint or use the existing `/ai` endpoint with a new prompt type. The gateway already supports chat completions with tool definitions.

## Phase 4: MCP Server

### Transport

stdio for local VS Code integration. User adds to `.vscode/mcp.json`:

```json
{
    "servers": {
        "salesbuddy": {
            "command": "python",
            "args": ["-m", "app.mcp_server"],
            "cwd": "C:\\dev\\SalesBuddy"
        }
    }
}
```

### Implementation

- Use the `mcp` Python SDK (`pip install mcp`)
- Register tools from the shared registry via `get_mcp_tools()`
- Tool handlers call the same `execute_tool()` dispatcher
- Needs a Flask app context since handlers use SQLAlchemy
- Read-only tools only (no write actions via MCP initially - too risky without UI confirmation)

### MCP Resources (Optional)

Expose SalesBuddy entities as MCP resources for richer context:

- `salesbuddy://customer/{id}` - Customer details
- `salesbuddy://engagement/{id}` - Engagement details
- `salesbuddy://milestone/{id}` - Milestone details

## Build Order

1. **Tool registry** (`copilot_tools.py`) with 3-4 read tools
2. **Chat endpoint** (`/api/ai/chat`) with tool execution loop
3. **Chat panel UI** (partial + JS) - get it working end to end
4. **Add more tools** based on what's actually useful in practice
5. **MCP server** - thin wrapper over the same tool registry
6. **Write tools with confirmation** - once the UX pattern is solid
7. **Conversation persistence** - if users want it

## Open Questions

- Should the chat panel be available on every page or only certain pages?
- Rate limiting on the chat endpoint? (Probably yes - same as other AI features)
- Should write actions go through the chat panel at all, or just read + summarize?
- MCP: stdio only, or also SSE for potential remote access?
