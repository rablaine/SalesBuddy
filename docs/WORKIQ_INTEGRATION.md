# WorkIQ Integration Guide for Sales Buddy

This document explains how we integrate with Microsoft WorkIQ to auto-populate notes from Teams meeting transcripts and summaries. The goal: click a button, select a meeting, get a pre-filled note with customer, date, discussion summary, and Azure topics extracted.

## Table of Contents

1. [What is WorkIQ?](#what-is-workiq)
2. [Prerequisites](#prerequisites)
3. [Authentication & Setup](#authentication--setup)
4. [Testing Access](#testing-access)
5. [Data Available from WorkIQ](#data-available-from-workiq)
6. [Integration Options](#integration-options)
7. [Sample Queries](#sample-queries)
8. [Mapping to Sales Buddy](#mapping-to-sales-buddy)
9. [What Didn't Work (Graph API)](#what-didnt-work-graph-api)
10. [Next Steps](#next-steps)

---

## What is WorkIQ?

[Microsoft WorkIQ](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/workiq-overview) is a CLI and MCP (Model Context Protocol) server that bridges AI assistants to your Microsoft 365 Copilot data. It exposes:

- **Meetings** - Calendar events, attendees, times
- **Transcripts** - Full meeting transcripts (when Copilot/transcription is enabled)
- **Meeting Summaries** - AI-generated summaries with action items
- **Emails** - Email threads and content
- **Documents** - SharePoint/OneDrive documents
- **Teams Messages** - Channel and chat messages

For Sales Buddy, we primarily care about **meetings** and **transcripts** to auto-populate notes.

---

## Prerequisites

### Required

1. **Microsoft 365 Copilot License** - WorkIQ requires a Copilot license to access meeting summaries
2. **Admin Consent** - WorkIQ needs admin consent in your tenant for the required Graph permissions
3. **Node.js** - Required to run the `npx` command
4. **VPN** - May be required depending on your org's network policies

### Check if Your Org Has WorkIQ Enabled

If someone in your org is already using WorkIQ, the admin consent is likely already done. Test it:

```bash
npx -y @microsoft/workiq version
```

If this returns a version number, you're good. First-time users must accept the EULA:

```bash
npx -y @microsoft/workiq accept-eula
```

---

## Authentication & Setup

### How WorkIQ Auth Works

WorkIQ uses **delegated authentication** - it runs as YOU, with YOUR permissions. When you run a query:

1. WorkIQ triggers a browser-based OAuth flow (first time only)
2. You sign in with your Microsoft corporate account
3. Token is cached locally
4. Subsequent queries use the cached token

**No app registration, client secrets, or redirect URIs needed.** WorkIQ's auth is handled by the package itself.

### Initial Setup

```bash
# Accept EULA (required once)
npx -y @microsoft/workiq accept-eula

# Test connectivity
npx -y @microsoft/workiq ask -q "What meetings do I have today?"
```

The first query will open a browser for authentication. After that, queries run headlessly.

---

## Testing Access

### Quick Connectivity Test

```bash
npx -y @microsoft/workiq ask -q "What meetings do I have today?"
```

### Test Meeting Summary Access

```bash
npx -y @microsoft/workiq ask -q "Summarize my most recent meeting with an external customer"
```

### If It Doesn't Work

1. **EULA not accepted**: Run `npx -y @microsoft/workiq accept-eula`
2. **Not authenticated**: First query should prompt browser login
3. **No Copilot license**: WorkIQ requires M365 Copilot - check with your admin
4. **Admin consent missing**: Your admin needs to consent to WorkIQ permissions

---

## Data Available from WorkIQ

### Meeting Data

| Data Point | Availability | Notes |
|------------|--------------|-------|
| Meeting title | ✅ Always | From calendar event |
| Date/time | ✅ Always | From calendar event |
| Attendees | ✅ Always | Internal and external |
| Organizer | ✅ Always | Who scheduled it |
| Recording | ✅ If recorded | Link to Teams recording |
| Transcript | ✅ If recorded | Full text transcript |
| AI Summary | ✅ If processed | Requires Copilot processing |
| Action items | ✅ If in summary | Extracted from discussion |
| External company | ✅ Inferred | From external attendee domains |

---

## Integration Options

### Option 1: MCP Server in VS Code (Recommended)

Add WorkIQ as an MCP server in VS Code so Claude/Copilot can query it directly:

```json
{
  "workiq": {
    "command": "npx",
    "args": ["-y", "@microsoft/workiq", "mcp"],
    "tools": ["*"]
  }
}
```

### Option 2: CLI Integration

Call WorkIQ from Python scripts:

```python
import subprocess

def query_workiq(question: str) -> str:
    cmd = f'npx -y @microsoft/workiq ask -q "{question}"'
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True, timeout=120)
    return result.stdout
```

---

## Sample Queries

### List Today's Meetings

```bash
npx -y @microsoft/workiq ask -q "List my meetings for today with title and time"
```

### Meeting Details for Note (250-word summary)

```bash
npx -y @microsoft/workiq ask -q "For the '[Meeting Title]' meeting on [Date], provide:
1) Date (YYYY-MM-DD format)
2) External company name
3) A 250-word summary of what was discussed
4) Azure/Microsoft technologies mentioned (comma-separated)
5) Action items (numbered list)"
```

### Extract Technologies

```bash
npx -y @microsoft/workiq ask -q "From the 'Customer X' meeting, extract all Azure/Microsoft technologies or products that were discussed."
```

---

## Mapping to Sales Buddy

### Data Mapping

| WorkIQ Data | Sales Buddy Field | Notes |
|-------------|------------------|-------|
| Meeting date | Call date | Direct mapping |
| External company | Customer | Match by name or create new |
| Meeting summary (~250 words) | Content | HTML formatted |
| Technologies discussed | Topics | Auto-suggest matching topics |
| Action items | Content (appended) | Add as bullet list |
| Microsoft attendees | Seller | Match by name |

### Topic Auto-Mapping

```python
TOPIC_MAPPING = {
    'SQL Server': 'SQL Server',
    'Azure SQL': 'Azure SQL Database',
    'Azure SQL Managed Instance': 'Azure SQL MI',
    'Azure Virtual Machines': 'Azure VMs',
    'Azure AI': 'Azure AI',
    'Azure OpenAI': 'Azure OpenAI',
    'Fabric': 'Microsoft Fabric',
    'Power BI': 'Power BI',
    'Synapse': 'Azure Synapse',
    'Databricks': 'Azure Databricks',
    'Cosmos DB': 'Azure Cosmos DB',
}
```

---

## What Didn't Work (Graph API)

Before WorkIQ, we tried direct Graph API access:

| Approach | Result | Why It Failed |
|----------|--------|---------------|
| Device Code Flow | ❌ Blocked | Admin policy blocks device code |
| Interactive Browser (MSAL) | ❌ Blocked | Conditional access requires WAM |
| WAM Broker (msal-broker) | ❌ Blocked | Device compliance check failed |
| Azure CLI | ⚠️ Partial | Works for CRM, blocked for Graph calendar scopes |

### Why WorkIQ Works

WorkIQ has **already been granted admin consent** at the tenant level. When you authenticate to WorkIQ, you're using Microsoft's pre-approved app registration, bypassing:
- App registration requirements
- Admin consent workflows
- Device compliance checks

---

## Next Steps

### Phase 1: MCP Integration (Easy Win)
- Add WorkIQ MCP config to VS Code
- Use Claude to query meetings during note creation

### Phase 2: "Import from Meeting" Button
- Add button to note form
- Query recent meetings with external attendees
- Pre-fill form fields from selection

### Phase 3: Calendar Integration
- On calendar view, show "Import" icon next to meetings with transcripts
- Click to pull meeting summary into note

---

## Files Created

| File | Purpose |
|------|---------|
| `scripts/workiq_test.py` | Test suite for WorkIQ queries |
| `scripts/workiq_import.py` | Import helper script |
| `scripts/workiq_mcp_config.json` | MCP server configuration |
| `docs/WORKIQ_INTEGRATION.md` | This document |

---

## Resources

- [WorkIQ Documentation](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/workiq-overview)
- [WorkIQ GitHub](https://github.com/microsoft/work-iq-mcp)
- [Model Context Protocol](https://modelcontextprotocol.io/)

---

*Last updated: February 24, 2026*
