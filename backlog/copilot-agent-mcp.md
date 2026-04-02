# Copilot Agent & MCP Server

## Overview

Build an in-app AI chat panel ("Copilot") backed by Azure OpenAI via the existing APIM gateway, plus an MCP server so VS Code Copilot can interact with SalesBuddy data. Both consume a shared tool registry so logic is never duplicated.

## Implementation Phases

### Phase 1: Tool Registry & Scaffolding - DONE

**Status:** Complete

Built `app/services/copilot_tools.py` with a `@tool` decorator pattern. 13 read-only tools covering all major entities (Customer, Note, Engagement, Milestone, Seller, Opportunity, Partner, ActionItem) and reports (Hygiene, Workload, What's New, Revenue Alerts, Whitespace).

Enforcement:
- `copilot-instructions.md` rule: "add a tool when adding a queryable entity or report"
- `tests/test_copilot_tools.py::TestToolCoverage` - fails if a core entity or report is missing a tool
- `tests/test_copilot_tools.py::TestToolExecution` - verifies tools actually run against the DB

Key exports: `get_openai_tools()`, `get_mcp_tools()`, `execute_tool(name, params)`

---

### Phase 2: Chat Endpoint (Backend + Gateway) - DONE

**Status:** Complete

Built the full chat pipeline: gateway `POST /v1/chat` endpoint with server-side system prompt construction, page validation (`VALID_PAGES` whitelist), and tool passthrough. Flask `POST /api/ai/chat` with multi-round tool-calling orchestration loop (max 3 rounds), local tool execution via the `copilot_tools` registry, and token usage accumulation across rounds. Added `chat_completion_with_tools()` to `openai_client.py` and `CHAT_SYSTEM_PROMPT` to `prompts.py`. 13 tests in `tests/test_ai_chat.py`. Deployed to APIM staging and verified end-to-end with live tool calls.

**Original goal:** `POST /api/ai/chat` - the user sends a message, the backend orchestrates tool calls via Azure OpenAI, returns a final answer.

#### 2a. Gateway changes (`infra/gateway/`)

The gateway currently proxies single-shot prompt completions. For chat with tool calling, it needs:

- **New endpoint: `POST /ai/chat`** - accepts a full messages array + tool definitions (not just a prompt string)
- Routes to Azure OpenAI Chat Completions API with `tools` parameter
- Returns the model's response including any `tool_calls`
- The gateway does NOT execute tools - it just relays the model's tool call requests back to the Flask app
- Auth: same Entra JWT validation as existing endpoints

This is the simplest approach: the gateway stays thin (just a relay), and tool execution happens in the Flask app which has DB access.

#### 2b. Flask chat endpoint (`app/routes/ai.py`)

New route: `POST /api/ai/chat`

Request body:
```json
{
    "message": "What milestones are at risk for Contoso?",
    "history": [{"role": "user", "content": "..."}, ...],
    "context": {"page": "customer_view", "customer_id": 42}
}
```

**Validation:**
- `context` is required. Requests without a valid `page` field are rejected with 400. This prevents use as a generic chat proxy since you'd need to fabricate Sales Buddy page context.
- `message` max length: 2000 characters.
- `history` max length: 20 messages (older messages truncated from the front).

Flow:
1. Build system prompt with persona + page context (see 2c)
2. Send messages + `get_openai_tools()` to gateway `/ai/chat`
3. If response contains `tool_calls`, execute each via `execute_tool()`
4. Send tool results back to gateway for a final response
5. Return `{"reply": "...", "tools_used": [...]}`

Tool execution loop should cap at 3 rounds to prevent runaway chains.

#### 2c. System prompt construction (abuse prevention)

The system prompt is the primary guardrail against misuse. The gateway constructs it - callers cannot override it.

Build dynamically per request:
- **Persona + scope lock:** "You are a Sales Buddy assistant for Azure technical sellers. You ONLY answer questions about customers, engagements, milestones, notes, revenue, partners, and seller workload tracked in Sales Buddy. Politely decline any unrelated requests - you are not a general-purpose assistant."
- **Page context injection:** "The user is currently viewing customer Contoso (TPID 12345)..." - constructed from the `context` field in the request. This grounds the model's responses in the user's current workflow.
- **Behavioral rules:** Be concise, cite data from tool results, don't hallucinate, say when data is missing, never fabricate customer names or numbers.
- **Tool-only data access:** "Only reference data returned by tool calls. Do not guess or infer data that wasn't returned."

#### 2d. Testing

- Unit tests for system prompt construction
- Unit tests for the tool execution loop (mock gateway responses with tool_calls)
- Integration test: send a chat message, verify response includes tool_used

---

### Phase 3: Chat Panel UI (Dev-Only)

**Status:** In Progress

**Detailed spec:** See `backlog/copilot-chat-flyout.md` for the full flyout design, stacking behavior, CSS overrides, and implementation plan.

**Goal:** A working chat panel in the browser, gated behind `FLASK_ENV=development` so it doesn't ship to production yet.

#### 3a. Toggle & panel shell

- Chat toggle button in navbar (sparkle/brain icon), only rendered when `FLASK_ENV == 'development'`
- Collapsible side panel (~450px), pinned to right edge
- Panel has: message input, send button, scrollable message area, close button

#### 3b. Message rendering

- User messages: right-aligned bubbles
- Assistant messages: left-aligned, rendered as Markdown (links, tables, lists, bold)
- Typing indicator (pulsing dots) while waiting for response
- Auto-scroll to newest message

#### 3c. Page context system

Each template emits a `window.copilotContext` object with page-specific data:
```js
window.copilotContext = {
    page: 'customer_view',
    customer_id: 42,
    customer_name: 'Contoso'
};
```

The chat JS includes this with every request. Context changes when the user navigates. Start with key pages: customer view, milestone tracker, engagement view, home dashboard.

#### 3d. Conversation management

- Message history stored in JS memory (resets on page navigation)
- History sent with each request so the model has conversational context
- Cap history at ~20 messages to stay within token limits
- "Clear conversation" button

#### 3e. Dev gate

- Template conditional: `{% if config.ENV == 'development' %}`
- Chat endpoint also checks `FLASK_ENV` and returns 404 in production
- Remove the gate in a future phase once the feature is stable

---

### Phase 4: More Entity & Report Tools

**Goal:** Fill gaps that become obvious once you can actually chat with the data.

Likely additions based on existing features:
- `get_milestone_status` enhancement: add `due_within_days` parameter for date-range filtering at the DB level (avoids LLM date math and result truncation)
- `get_territory_summary` - territory view data (customers, sellers, recent activity)
- `get_pod_overview` - POD structure with territories and solution engineers
- `get_analytics_summary` - call volume metrics, top topics, customers needing attention
- `report_one_on_one` - 1:1 manager prep data (recent customer activity, commitments, topic trends)
- `search_contacts` - search customer and partner contacts across the whole database
- `get_revenue_customer_detail` - per-customer revenue history and bucket breakdown

Each tool follows the same pattern: decorate a function in `copilot_tools.py`, call existing query code, return JSON-serializable dict. Add the keyword to `TestToolCoverage` if it covers a new entity.

---

### Phase 5: MCP Server

**Goal:** Let VS Code Copilot query SalesBuddy data directly via MCP protocol.

#### 5a. Server implementation (`app/mcp_server.py`)

- Uses the `mcp` Python SDK (`pip install mcp`)
- Registers tools from `get_mcp_tools()`
- Tool handlers call `execute_tool()` within a Flask app context
- stdio transport for local VS Code usage
- Read-only tools only (no write actions without UI confirmation)

#### 5b. VS Code integration

User adds to `.vscode/mcp.json`:
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

Then VS Code Copilot can call tools like `search_notes`, `get_customer_summary`, etc. directly in chat.

#### 5c. MCP Resources (stretch)

Expose entities as MCP resources for richer context:
- `salesbuddy://customer/{id}`
- `salesbuddy://engagement/{id}`
- `salesbuddy://milestone/{id}`

---

### Phase 6: Write Tools with Confirmation

**Goal:** Let the chat modify data (update engagement status, add comments, create notes) with a confirmation step.

- Write tools get a `requires_confirmation: true` flag in the registry
- Chat endpoint returns pending actions instead of executing immediately
- Panel shows a confirmation card ("I'll mark Fabric POC as Won. Confirm?")
- User clicks confirm, panel sends `POST /api/ai/chat/confirm` to execute
- Requires careful UX - only after the read-only experience is solid

---

## Open Questions

- Should the chat panel be available on every page or only certain pages?
- Rate limiting on the chat endpoint? (Probably yes - same as other AI features)
- MCP: stdio only, or also SSE for potential remote access?

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

## Resume Context

**Last completed:** Phase 1 (tool registry) - merged to main.

**Next up:** Phase 2 (chat endpoint). Start with 2a (gateway `POST /ai/chat`) then 2b (Flask `POST /api/ai/chat` with tool execution loop).

**Key design decisions made:**
- System prompt is the primary abuse guardrail (scope-locked persona, topic restriction)
- `context` field is required on all chat requests (prevents generic proxy use)
- Gateway stays thin (relay only) - tool execution happens in Flask which has DB access
- Chat panel is dev-gated (`FLASK_ENV=development`) in Phase 3

**Existing gateway pattern to follow:** See `infra/gateway/gateway.py` for current endpoints and `app/gateway_client.py` for how the Flask app calls the gateway. The new `/ai/chat` endpoint follows the same auth pattern (Entra JWT via APIM).

**13 tools already registered in `app/services/copilot_tools.py`:**
search_customers, get_customer_summary, search_notes, get_engagement_details, get_milestone_status, get_seller_workload, get_opportunity_details, search_partners, list_action_items, report_hygiene, report_workload, report_whats_new, report_revenue_alerts, report_whitespace

## Architecture Reference
