# APIM Gateway Implementation Plan

> **Issue:** [#38 — Host APIM endpoint for centralized OpenAI integration](https://github.com/rablaine/NoteHelper/issues/38)
> **Last updated:** 2026-03-07

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Azure Resources](#azure-resources)
4. [Endpoint Contracts](#endpoint-contracts)
5. [Phase 1A — Provision Azure Resources](#phase-1a--provision-azure-resources)
6. [Phase 1B — Function App Code](#phase-1b--function-app-code)
7. [Phase 1C — APIM Configuration](#phase-1c--apim-configuration)
8. [Phase 2 — NoteHelper Code Changes](#phase-2--notehelper-code-changes)
9. [Security Model](#security-model)
10. [Quota & Rate Limiting](#quota--rate-limiting)
11. [Telemetry](#telemetry)
12. [Future: Token-Based Quotas](#future-token-based-quotas)

---

## Overview

NoteHelper currently calls Azure OpenAI directly from each user's machine using a service principal with credentials stored in `.env`. This plan replaces that with a centralized **APIM → Function App → Azure OpenAI** gateway hosted in an external (non-Microsoft) Azure tenant.

### Goals

- **Eliminate user-side OpenAI setup** — no more 5 env vars per user.
- **Prevent abuse** — prompts are constructed server-side; endpoints are purpose-built and cannot be repurposed as a general OpenAI proxy.
- **Microsoft employee validation** — JWT tenant-level check (Microsoft tenant ID).
- **Per-user quotas** — APIM-native rate limiting keyed on user `oid` claim.
- **Future-ready** — Function App layer enables token-budget enforcement later without client changes.

### What's NOT in scope

- VNet / private endpoints (can be added if Microsoft adopts this corporately).
- Subscription keys or user-configured gateway URLs (hardcoded in app).
- Content logging in the gateway (only metadata — no prompts or user data).
- Changes to WorkIQ (local CLI tool, not OpenAI — unaffected).

---

## Architecture

```
NoteHelper (Flask, localhost)
    │
    │  HTTPS + Bearer JWT (user's Entra ID token)
    │  POST /v1/suggest-topics  { call_notes: "..." }
    ▼
APIM (Consumption tier, external tenant)
    │  ── validate-jwt (tid == Microsoft tenant ID)
    │  ── rate-limit-by-key (keyed on oid claim)
    │  ── quota-by-key (keyed on oid claim)
    │  ── route to Function App backend
    ▼
Azure Function App (Python, HTTP triggers, external tenant)
    │  ── Receives sanitized input (user data only)
    │  ── Builds full prompt from hardcoded templates
    │  ── Calls Azure OpenAI via Managed Identity
    │  ── Parses and validates OpenAI response
    │  ── Returns structured JSON
    ▼
Azure OpenAI / Foundry (gpt-4o-mini, external tenant)
```

### Why Function App + APIM (not APIM alone)

If APIM passed user-supplied prompts directly to OpenAI, anyone with a valid JWT could use the endpoint as a free-form GPT proxy. By moving prompt construction into the Function App:

- Each endpoint accepts **only structured user data** (call notes, milestone list, etc.).
- The **system prompt is hardcoded server-side** — users never had control over these prompts and still don't.
- Endpoints return **narrow, structured JSON** (topic arrays, milestone IDs, summaries in specific formats).
- There is **no way to repurpose** a suggest-topics endpoint into a general chat interface.

This is fundamentally stronger than any rate limit or subscription key approach.

---

## Azure Resources

All resources live in a single resource group in the external tenant.

| Resource | SKU / Tier | Purpose |
|---|---|---|
| **Resource Group** | `rg-notehelper-ai-gateway` | Container for all gateway resources |
| **Azure Key Vault** | Standard | Store OpenAI API key / Foundry connection string |
| **Azure Function App** | Python 3.11+, Consumption plan | Prompt construction, OpenAI calls, response parsing |
| **Azure API Management** | Consumption | JWT validation, rate limiting, routing, diagnostics |
| **Entra ID App Registration** | — | Defines the audience/scope for gateway JWTs |
| **Azure OpenAI / Foundry** | gpt-4o-mini | Already exists — the model endpoint |
| **Application Insights** | — | Already exists — wire APIM diagnostics here |

### Identity chain

```
Function App (system-assigned Managed Identity)
    ├── Key Vault: GET secrets (OpenAI key)
    └── Azure OpenAI: "Cognitive Services OpenAI User" role

APIM
    └── Function App backend (function key or Managed Identity)
```

---

## Endpoint Contracts

Each endpoint has a narrow, purpose-built input/output contract. No system prompt, model selection, or max_tokens is exposed to callers.

### 1. `POST /v1/suggest-topics`

Suggests topic tags from call notes.

**NoteHelper caller:** `POST /api/ai/suggest-topics` in `app/routes/ai.py`
**Server-side prompt:** `TOPIC_SUGGESTION_PROMPT`

```json
// Request
{
  "call_notes": "Discussed Azure OpenAI integration and RAG patterns..."
}

// Response
{
  "success": true,
  "topics": ["Azure OpenAI", "RAG Pattern", "Vector Search"]
}
```

### 2. `POST /v1/match-milestone`

Matches call notes to the most relevant milestone from a provided list.

**NoteHelper caller:** `POST /api/ai/match-milestone` in `app/routes/ai.py`
**Server-side prompt:** Inline system prompt (milestone matching instructions)

```json
// Request
{
  "call_notes": "Discussed migrating their SQL workloads to Azure...",
  "milestones": [
    {"id": "ms-123", "name": "SQL Migration", "status": "Active", "opportunity": "Contoso", "workload": "SQL"},
    {"id": "ms-456", "name": "AKS Onboarding", "status": "Active", "opportunity": "Contoso", "workload": "Kubernetes"}
  ]
}

// Response
{
  "success": true,
  "milestone_id": "ms-123",
  "reason": "The call specifically discussed SQL workload migration to Azure"
}
```

### 3. `POST /v1/analyze-call`

Extracts topic tags from call notes (auto-fill flow).

**NoteHelper caller:** `POST /api/ai/analyze-call` in `app/routes/ai.py`
**Server-side prompt:** Inline system prompt (topic extraction instructions)

```json
// Request
{
  "call_notes": "Reviewed their Kubernetes cluster performance and discussed cost optimization..."
}

// Response
{
  "success": true,
  "topics": ["Azure Kubernetes Service", "Cost Optimization"]
}
```

### 4. `POST /v1/engagement-summary`

Generates a structured engagement summary from all call logs for a customer.

**NoteHelper caller:** `POST /api/ai/generate-engagement-summary` in `app/routes/ai.py`
**Server-side prompt:** `ENGAGEMENT_SUMMARY_PROMPT`

```json
// Request
{
  "customer_name": "Contoso",
  "tpid": "12345",
  "overview": "Existing customer overview text...",
  "notes": [
    {"date": "2026-01-15", "content": "Discussed migration timeline...", "topics": ["SQL", "Migration"]},
    {"date": "2026-02-10", "content": "Reviewed AKS architecture...", "topics": ["AKS"]}
  ]
}

// Response
{
  "success": true,
  "summary": "Key Individuals & Titles: ...\nTechnical/Business Problem: ...\nBusiness Process/Strategy: ...\nSolution Resources: ...\nBusiness Outcome in Estimated $$ACR: ...\nFuture Date/Timeline: ...\nRisks/Blockers: ..."
}
```

### 5. `POST /v1/connect-summary`

Generates Connect self-evaluation narrative. Supports single-call, chunk, and synthesis modes for large exports.

**NoteHelper caller:** `_call_openai()` in `app/routes/connect_export.py`
**Server-side prompts:** `CONNECT_SUMMARY_SYSTEM_PROMPT`, `CONNECT_CHUNK_SYSTEM_PROMPT`, `CONNECT_SYNTHESIS_SYSTEM_PROMPT`

```json
// Request
{
  "mode": "single",
  "text_export": "Full export text...",
  "header": "Period stats header..."
}

// Request (chunked)
{
  "mode": "chunk",
  "customer_text": "Customer details for this chunk...",
  "header": "Period stats header...",
  "general_notes_text": "General notes (last chunk only)...",
  "chunk_index": 1,
  "chunk_count": 3
}

// Request (synthesis)
{
  "mode": "synthesis",
  "partial_summaries": ["Chunk 1 summary...", "Chunk 2 summary...", "Chunk 3 summary..."],
  "header": "Period stats header...",
  "chunk_count": 3
}

// Response (all modes)
{
  "success": true,
  "summary": "## What results did you deliver...",
  "usage": {
    "model": "gpt-4o-mini",
    "prompt_tokens": 1200,
    "completion_tokens": 800,
    "total_tokens": 2000
  }
}
```

### 6. `POST /v1/ping`

Health check / connection test.

**NoteHelper caller:** `POST /api/admin/ai-config/test` in `app/routes/admin.py`

```json
// Request
{}

// Response
{
  "success": true,
  "status": "ok"
}
```

---

## Phase 1A — Provision Azure Resources

### Option 1: Azure Portal

1. **Resource Group**
   - Create `rg-notehelper-ai-gateway` in your preferred region.

2. **Key Vault**
   - Create `kv-notehelper-ai` in the resource group.
   - Add a secret `openai-api-key` with your Foundry/OpenAI key (or store the full endpoint connection info — whatever your Foundry setup needs).

3. **Function App**
   - Create a Function App: Python 3.11+, Consumption plan, in the resource group.
   - Name suggestion: `func-notehelper-ai`
   - Enable **system-assigned Managed Identity** under Identity.
   - Go to Key Vault → Access policies → Add the Function App's MI with **Get** secret permission.
   - Go to your Azure OpenAI resource → IAM → Add role assignment → **Cognitive Services OpenAI User** → assign to the Function App's MI.

4. **Entra ID App Registration**
   - Register a new app: `NoteHelper AI Gateway`
   - Set the **Application ID URI** (e.g., `api://notehelper-ai-gateway`).
   - Add a scope: `api://notehelper-ai-gateway/access` (admin consent).
   - Under **Expose an API**, add the NoteHelper client app ID as an authorized client.
   - Note the **Application (client) ID** — this is the `audience` for JWT validation.

5. **APIM Instance**
   - Create APIM with Consumption tier in the resource group.
   - Name suggestion: `apim-notehelper-ai`
   - Under Settings → Diagnostics, connect to your existing Application Insights instance.

### Option 2: Azure CLI

```bash
# Variables
RG="rg-notehelper-ai-gateway"
LOCATION="eastus"
KV_NAME="kv-notehelper-ai"
FUNC_NAME="func-notehelper-ai"
APIM_NAME="apim-notehelper-ai"
STORAGE_NAME="stnotehelperai"  # must be globally unique, lowercase, no hyphens
OPENAI_RESOURCE_ID="/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<name>"
APPINSIGHTS_KEY="<your-existing-instrumentation-key>"

# 1. Resource Group
az group create --name $RG --location $LOCATION

# 2. Key Vault
az keyvault create --name $KV_NAME --resource-group $RG --location $LOCATION
az keyvault secret set --vault-name $KV_NAME --name "openai-api-key" --value "<your-key>"

# 3. Storage Account (required by Function App)
az storage account create --name $STORAGE_NAME --resource-group $RG --location $LOCATION --sku Standard_LRS

# 4. Function App
az functionapp create \
  --name $FUNC_NAME \
  --resource-group $RG \
  --storage-account $STORAGE_NAME \
  --consumption-plan-location $LOCATION \
  --runtime python \
  --runtime-version 3.11 \
  --os-type Linux \
  --functions-version 4 \
  --app-insights-key $APPINSIGHTS_KEY

# 5. Enable Managed Identity
az functionapp identity assign --name $FUNC_NAME --resource-group $RG

# Get the MI principal ID
MI_PRINCIPAL=$(az functionapp identity show --name $FUNC_NAME --resource-group $RG --query principalId -o tsv)

# 6. Grant MI access to Key Vault
az keyvault set-policy --name $KV_NAME --object-id $MI_PRINCIPAL --secret-permissions get

# 7. Grant MI access to Azure OpenAI
az role assignment create \
  --assignee $MI_PRINCIPAL \
  --role "Cognitive Services OpenAI User" \
  --scope $OPENAI_RESOURCE_ID

# 8. APIM (Consumption tier)
az apim create \
  --name $APIM_NAME \
  --resource-group $RG \
  --location $LOCATION \
  --publisher-name "NoteHelper" \
  --publisher-email "admin@yourdomain.com" \
  --sku-name Consumption

# 9. Entra ID App Registration
az ad app create \
  --display-name "NoteHelper AI Gateway" \
  --identifier-uris "api://notehelper-ai-gateway" \
  --sign-in-audience AzureADMultipleOrgs
```

---

## Phase 1B — Function App Code

The Function App is a Python project with one HTTP-trigger function per endpoint plus a shared OpenAI client module.

### Project structure

```
func-notehelper-ai/
├── function_app.py          # Main function app with all HTTP triggers
├── shared/
│   ├── __init__.py
│   ├── openai_client.py     # Azure OpenAI client (Managed Identity)
│   └── prompts.py           # All system prompt templates (copied from NoteHelper)
├── host.json
├── local.settings.json
└── requirements.txt
```

### `shared/openai_client.py` — OpenAI client via Managed Identity

```python
import os
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

_client = None

def get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        _client = AzureOpenAI(
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token_provider,
        )
    return _client

def get_deployment() -> str:
    return os.environ["AZURE_OPENAI_DEPLOYMENT"]

def chat_completion(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> dict:
    """Make a chat completion call and return structured result."""
    client = get_client()
    deployment = get_deployment()

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        model=deployment,
    )

    text = response.choices[0].message.content or ""
    usage = {
        "model": response.model or deployment,
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "total_tokens": response.usage.total_tokens if response.usage else 0,
    }
    return {"text": text.strip(), "usage": usage}
```

### `shared/prompts.py` — System prompt templates

Copy these directly from the NoteHelper codebase (`app/routes/ai.py` and `app/routes/connect_export.py`):

```python
# From app/routes/ai.py
TOPIC_SUGGESTION_PROMPT = (
    "You are a helpful assistant that analyzes call notes and suggests relevant topic tags. "
    "Based on the call notes provided, return a JSON array of 3-7 short topic tags (1-3 words each) "
    "that best describe the key technologies, products, or themes discussed. "
    "Return ONLY a JSON array of strings, nothing else. "
    'Example: ["Azure OpenAI", "Vector Search", "RAG Pattern"]'
)

MILESTONE_MATCH_PROMPT = """You are an expert at matching customer call notes to sales milestones.
Your task is to identify which milestone best matches the topics discussed in the call notes.

Respond with ONLY a JSON object in this exact format (no markdown, no explanation):
{"milestone_id": "THE_MATCHED_ID", "reason": "Brief explanation of why this milestone matches"}

If no milestone is a good match, respond with:
{"milestone_id": null, "reason": "No milestone matches the call discussion"}"""

ANALYZE_CALL_PROMPT = """You are an expert at analyzing Azure customer call notes.
Extract the key technologies and concepts discussed.

Respond with ONLY a JSON object in this exact format (no markdown, no explanation):
{
  "topics": ["Topic 1", "Topic 2", "Topic 3"]
}

Guidelines:
- topics: List 2-5 Azure/Microsoft technologies or concepts discussed
- Focus on specific, actionable technology areas rather than generic terms"""

ENGAGEMENT_SUMMARY_PROMPT = (
    "You are a Microsoft technical seller's assistant. Analyze the provided notes "
    "and any existing customer overview for a customer and generate a structured engagement summary. "
    "Fill in each field based on what you can extract from the notes. If a field "
    "cannot be determined from the available information, write 'Not identified in notes' "
    "for that field.\n\n"
    "Return your response in EXACTLY this format (keep the field labels exactly as shown, "
    "fill in the values after the colon):\n\n"
    "Key Individuals & Titles: [names and titles of key people mentioned]\n"
    "Technical/Business Problem: [the technical or business challenges they face]\n"
    "Business Process/Strategy: [how the problem impacts their business]\n"
    "Solution Resources: [Azure services, tools, or approaches being used to address it]\n"
    "Business Outcome in Estimated $$ACR: [expected revenue impact or business value]\n"
    "Future Date/Timeline: [any deadlines, milestones, or target dates mentioned]\n"
    "Risks/Blockers: [any risks, blockers, or concerns raised]\n\n"
    "Be concise but specific. Use actual details from the notes, not generic "
    "statements. If multiple topics or workstreams exist, cover the most significant ones."
)

# From app/routes/connect_export.py
CONNECT_SUMMARY_SYSTEM_PROMPT = (
    "You are an expert at writing Microsoft Connect self-evaluations for "
    "Azure technical sellers. You will receive structured note data "
    "covering a specific date range. Your job is to produce content the "
    "seller can paste directly into their Connect form.\n\n"
    "The Connect form has 3 fields. Write each as a separate Markdown section:\n\n"
    "## What results did you deliver, and how did you do it?\n"
    "- Focus on IMPACT, not just activity. Highlight outcomes and results.\n"
    "- Use specific examples with metrics where possible.\n"
    "- Demonstrate WHAT you delivered and HOW you worked.\n\n"
    "## Reflect on setbacks - what did you learn?\n"
    "- Be honest and self-aware based on what the data shows.\n"
    "- If data is thin in some areas, note it constructively.\n\n"
    "## What are your priorities going forward?\n"
    "- Base these on patterns you see in the data.\n"
    "- Suggest concrete next steps, not vague aspirations.\n\n"
    "Tips (follow these strictly):\n"
    "- Be concise. Use bullet points, not paragraphs.\n"
    "- Quantify your impact wherever you can.\n"
    "- Avoid routine tasks. Focus on outcomes that moved the needle.\n"
    "- Write in first person ('I engaged...', 'I helped...').\n"
    "- Do not invent information that isn't in the data.\n"
)

CONNECT_CHUNK_SYSTEM_PROMPT = (
    "You are an expert at writing Microsoft Connect self-evaluations for "
    "Azure technical sellers. You will receive a subset of note data for "
    "specific customers. Summarize the engagements for ONLY the customers in "
    "this chunk.\n\n"
    "Write concise bullet points covering:\n"
    "- Key results and impact per customer (with metrics where available)\n"
    "- Technologies discussed and outcomes\n"
    "- Revenue impact where applicable\n"
    "- Any gaps or areas for improvement you can identify\n\n"
    "Tips (follow these strictly):\n"
    "- Be concise. Use bullet points, not paragraphs.\n"
    "- Quantify impact wherever you can.\n"
    "- Focus on outcomes that moved the needle, not routine tasks.\n"
    "- Write in first person.\n"
    "- Do not invent information that isn't in the data.\n"
)

CONNECT_SYNTHESIS_SYSTEM_PROMPT = (
    "You are an expert at writing Microsoft Connect self-evaluations for "
    "Azure technical sellers. You will receive multiple partial summaries "
    "that each cover a subset of customers, plus overall statistics. "
    "Combine them into a single Connect form response.\n\n"
    "The Connect form has 3 fields. Write each as a separate Markdown section:\n\n"
    "## What results did you deliver, and how did you do it?\n"
    "- Focus on IMPACT, not just activity. Highlight outcomes and results.\n"
    "- Use specific examples with metrics where possible.\n"
    "- Demonstrate WHAT you delivered and HOW you worked.\n\n"
    "## Reflect on setbacks - what did you learn?\n"
    "- Be honest and self-aware based on what the data shows.\n"
    "- If data is thin in some areas, note it constructively.\n\n"
    "## What are your priorities going forward?\n"
    "- Base these on patterns you see in the data.\n"
    "- Suggest concrete next steps, not vague aspirations.\n\n"
    "Tips (follow these strictly):\n"
    "- Be concise. Use bullet points, not paragraphs.\n"
    "- Quantify your impact wherever you can.\n"
    "- Avoid routine tasks. Focus on outcomes that moved the needle.\n"
    "- Write in first person.\n"
    "- Do not invent information that isn't in the data.\n"
)
```

### `function_app.py` — Example HTTP trigger (suggest-topics)

```python
import azure.functions as func
import json
import logging
import re

from shared.openai_client import chat_completion
from shared.prompts import (
    TOPIC_SUGGESTION_PROMPT,
    MILESTONE_MATCH_PROMPT,
    ANALYZE_CALL_PROMPT,
    ENGAGEMENT_SUMMARY_PROMPT,
    CONNECT_SUMMARY_SYSTEM_PROMPT,
    CONNECT_CHUNK_SYSTEM_PROMPT,
    CONNECT_SYNTHESIS_SYSTEM_PROMPT,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="v1/suggest-topics", methods=["POST"])
def suggest_topics(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        call_notes = body.get("call_notes", "").strip()

        if not call_notes or len(call_notes) < 10:
            return func.HttpResponse(
                json.dumps({"success": False, "error": "Call notes too short"}),
                status_code=400, mimetype="application/json"
            )

        result = chat_completion(TOPIC_SUGGESTION_PROMPT, f"Call notes:\n\n{call_notes}", max_tokens=150)

        # Parse JSON array from response
        text = result["text"]
        clean = text
        if "```" in clean:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
            if match:
                clean = match.group(1).strip()
        array_match = re.search(r"\[.*\]", clean, re.DOTALL)
        if array_match:
            clean = array_match.group(0)
        topics = json.loads(clean)

        return func.HttpResponse(
            json.dumps({"success": True, "topics": topics, "usage": result["usage"]}),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"suggest-topics error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )


@app.route(route="v1/match-milestone", methods=["POST"])
def match_milestone(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        call_notes = body.get("call_notes", "").strip()
        milestones = body.get("milestones", [])

        if not call_notes or len(call_notes) < 20:
            return func.HttpResponse(
                json.dumps({"success": False, "error": "Call notes too short"}),
                status_code=400, mimetype="application/json"
            )
        if not milestones:
            return func.HttpResponse(
                json.dumps({"success": False, "error": "No milestones provided"}),
                status_code=400, mimetype="application/json"
            )

        milestone_list = "\n".join([
            f"- ID: {{m.get('id')}}, Name: {{m.get('name')}}, Status: {{m.get('status')}}, "
            f"Opportunity: {{m.get('opportunity', '')}}, Workload: {{m.get('workload', '')}}"
            for m in milestones
        ])
        user_prompt = f"Call Notes:\n{{call_notes[:2000]}}\n\nAvailable Milestones:\n{{milestone_list}}\n\nWhich milestone best matches what was discussed in the call?"

        result = chat_completion(MILESTONE_MATCH_PROMPT, user_prompt, max_tokens=150)

        # Parse JSON response
        text = result["text"]
        clean = text
        if "```" in clean:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
            if match:
                clean = match.group(1).strip()
        parsed = json.loads(clean)

        return func.HttpResponse(
            json.dumps({
                "success": True,
                "milestone_id": parsed.get("milestone_id"),
                "reason": parsed.get("reason", ""),
                "usage": result["usage"]
            }),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"match-milestone error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )


@app.route(route="v1/analyze-call", methods=["POST"])
def analyze_call(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        call_notes = body.get("call_notes", "").strip()

        if not call_notes or len(call_notes) < 20:
            return func.HttpResponse(
                json.dumps({"success": False, "error": "Call notes too short"}),
                status_code=400, mimetype="application/json"
            )

        user_prompt = f"Analyze these call notes and extract the key topics/technologies discussed:\n\n{{call_notes[:3000]}}"
        result = chat_completion(ANALYZE_CALL_PROMPT, user_prompt, max_tokens=200)

        text = result["text"]
        clean = text
        if "```" in clean:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
            if match:
                clean = match.group(1).strip()
        parsed = json.loads(clean)

        return func.HttpResponse(
            json.dumps({"success": True, "topics": parsed.get("topics", []), "usage": result["usage"]}),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"analyze-call error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )


@app.route(route="v1/engagement-summary", methods=["POST"])
def engagement_summary(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        customer_name = body.get("customer_name", "")
        tpid = body.get("tpid", "")
        overview = body.get("overview", "")
        notes = body.get("notes", [])

        if not notes:
            return func.HttpResponse(
                json.dumps({"success": False, "error": "No notes provided"}),
                status_code=400, mimetype="application/json"
            )

        # Build call text (same logic as app/routes/ai.py)
        call_text_parts = []
        for n in notes:
            entry = f"[{{n.get('date', '')}}]"
            topics = n.get("topics", [])
            if topics:
                entry += f" Topics: {{', '.join(topics)}}"
            entry += f"\n{{n.get('content', '')}}"
            call_text_parts.append(entry)
        call_text = "\n\n---\n\n".join(call_text_parts)

        # Cap input
        MAX_CHARS = 30000
        if len(call_text) > MAX_CHARS:
            call_text = call_text[:MAX_CHARS] + "\n\n[... additional notes truncated ...]"

        notes_section = f"\nExisting Customer Notes:\n{{overview}}\n" if overview else ""
        user_message = (
            f"Customer: {{customer_name}} (TPID: {{tpid}})\n"
            f"Total notes: {{len(notes)}}\n"
            f"{{notes_section}}\n"
            f"Notes:\n\n{{call_text}}"
        )

        result = chat_completion(ENGAGEMENT_SUMMARY_PROMPT, user_message, max_tokens=1000)

        return func.HttpResponse(
            json.dumps({"success": True, "summary": result["text"], "usage": result["usage"]}),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"engagement-summary error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )


@app.route(route="v1/connect-summary", methods=["POST"])
def connect_summary(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        mode = body.get("mode", "single")

        if mode == "single":
            text_export = body.get("text_export", "")
            user_prompt = (
                "Here is my note data for this Connect period.  "
                "Please write my Connect self-evaluation.\n\n"
                f"{{text_export}}"
            )
            result = chat_completion(CONNECT_SUMMARY_SYSTEM_PROMPT, user_prompt, max_tokens=2000)

        elif mode == "chunk":
            header = body.get("header", "")
            customer_text = body.get("customer_text", "")
            general_notes_text = body.get("general_notes_text", "")
            chunk_index = body.get("chunk_index", 1)
            chunk_count = body.get("chunk_count", 1)
            user_prompt = (
                f"Overall period stats:\n{{header}}\n\n"
                f"Customer details (chunk {{chunk_index}} of {{chunk_count}}):\n\n"
                f"{{customer_text}}{{general_notes_text}}"
            )
            result = chat_completion(CONNECT_CHUNK_SYSTEM_PROMPT, user_prompt, max_tokens=1500)

        elif mode == "synthesis":
            header = body.get("header", "")
            partial_summaries = body.get("partial_summaries", [])
            chunk_count = body.get("chunk_count", len(partial_summaries))
            combined = "\n\n---\n\n".join(
                f"### Chunk {{i + 1}}\n{{s}}" for i, s in enumerate(partial_summaries)
            )
            user_prompt = (
                f"Overall period stats:\n{{header}}\n\n"
                f"Here are partial summaries from {{chunk_count}} customer groups:\n\n"
                f"{{combined}}"
            )
            result = chat_completion(CONNECT_SYNTHESIS_SYSTEM_PROMPT, user_prompt, max_tokens=2000)

        else:
            return func.HttpResponse(
                json.dumps({"success": False, "error": f"Invalid mode: {{mode}}"}),
                status_code=400, mimetype="application/json"
            )

        return func.HttpResponse(
            json.dumps({"success": True, "summary": result["text"], "usage": result["usage"]}),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"connect-summary error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )

@app.route(route="v1/ping", methods=["POST"])
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """Health check — verifies the Function can reach OpenAI."""
    try:
        result = chat_completion(
            "You are a helpful assistant.",
            "Say 'Connection successful!' and nothing else.",
            max_tokens=20,
        )
        return func.HttpResponse(
            json.dumps({"success": True, "status": "ok", "response": result["text"]}),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"ping error: {e}")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status_code=500, mimetype="application/json"
        )
```

### `requirements.txt`

```
azure-functions
openai>=1.0
azure-identity
```

---

## Phase 1C — APIM Configuration

### 1. Import Function App as backend

In APIM → APIs → Add API → Function App → select `func-notehelper-ai` → import all functions.

### 2. Inbound policy: JWT validation + rate limiting

Apply this policy at the **API level** (all operations inherit it):

```xml
<policies>
    <inbound>
        <base />

        <!-- 1. Validate JWT — Microsoft tenant only -->
        <validate-jwt header-name="Authorization" require-scheme="Bearer" failed-validation-httpcode="401">
            <openid-config url="https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration" />
            <audiences>
                <audience>api://notehelper-ai-gateway</audience>
            </audiences>
            <required-claims>
                <claim name="tid" match="any">
                    <!-- Microsoft corporate tenant ID -->
                    <value>72f988bf-86f1-41af-91ab-2d7cd011db47</value>
                    <!-- Add your external tenant ID here if needed -->
                    <!-- <value>your-external-tenant-id</value> -->
                </claim>
            </required-claims>
        </validate-jwt>

        <!-- 2. Extract user OID for rate limiting -->
        <set-variable name="user-oid" value="@(context.Request.Headers.GetValueOrDefault("Authorization","" ).Split(' ').Last().AsJwt()?.Claims.GetValueOrDefault("oid", "unknown"))" />

        <!-- 3. Per-user rate limit: 20 calls per minute -->
        <rate-limit-by-key calls="20" renewal-period="60"
            counter-key="@((string)context.Variables["user-oid"])" />

        <!-- 4. Per-user daily quota: 200 calls per day -->
        <quota-by-key calls="200" renewal-period="86400"
            counter-key="@((string)context.Variables["user-oid"])" />
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>
```

### 3. Per-endpoint overrides (optional)

For expensive endpoints like `connect-summary`, add an **operation-level** policy:

```xml
<policies>
    <inbound>
        <base />
        <!-- Tighter limit for connect-summary: 10 calls per day -->
        <quota-by-key calls="10" renewal-period="86400"
            counter-key="@((string)context.Variables["user-oid"] + "-connect")" />
    </inbound>
</policies>
```

### 4. Test with curl

```bash
# Get a token (using Azure CLI for testing)
TOKEN=$(az account get-access-token --resource api://notehelper-ai-gateway --query accessToken -o tsv)

# Test ping
curl -X POST https://apim-notehelper-ai.azure-api.net/v1/ping \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'

# Test suggest-topics
curl -X POST https://apim-notehelper-ai.azure-api.net/v1/suggest-topics \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"call_notes": "Discussed migrating SQL Server workloads to Azure SQL Managed Instance and setting up Azure Data Factory pipelines for ETL."}'
```

---

## Phase 2 — NoteHelper Code Changes

### 1. New module: `app/services/ai_gateway.py`

Thin HTTP client that calls the APIM gateway.

```python
"""
AI Gateway client for centralized APIM-based OpenAI integration.
Replaces direct Azure OpenAI calls when AI_MODE=gateway.
"""
import requests
import logging
from flask import session

logger = logging.getLogger(__name__)

# Hardcoded gateway URL — no user configuration needed
GATEWAY_BASE_URL = "https://apim-notehelper-ai.azure-api.net"

def _get_user_token() -> str:
    """Get the user's Entra ID access token from the session/MSAL cache.

    The token audience must be the gateway app registration
    (api://notehelper-ai-gateway).
    """
    # Implementation depends on your MSAL integration.
    # The app already has Entra ID auth — acquire a token for the gateway scope.
    # Example: use MSAL confidential client's acquire_token_on_behalf_of()
    # or acquire_token_silent() with scope "api://notehelper-ai-gateway/access"
    raise NotImplementedError("Wire up MSAL token acquisition for gateway scope")

def call_gateway(endpoint: str, payload: dict) -> dict:
    """Call an APIM gateway endpoint.

    Args:
        endpoint: e.g., "/v1/suggest-topics"
        payload: JSON body to send

    Returns:
        Parsed JSON response dict

    Raises:
        requests.HTTPError: on 4xx/5xx responses
    """
    url = f"{{GATEWAY_BASE_URL}}{{endpoint}}"
    token = _get_user_token()

    headers = {
        "Authorization": f"Bearer {{token}}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()
```

### 2. Config switch in `app/routes/ai.py`

```python
import os

def get_ai_mode() -> str:
    """Get AI mode: 'gateway' (centralized APIM) or 'direct' (local service principal)."""
    return os.environ.get('AI_MODE', 'gateway')

def is_ai_enabled() -> bool:
    if get_ai_mode() == 'gateway':
        # Gateway mode: user just needs to be signed in
        return True
    else:
        # Direct mode: existing check
        return bool(
            os.environ.get('AZURE_OPENAI_ENDPOINT')
            and os.environ.get('AZURE_OPENAI_DEPLOYMENT')
        )
```

### 3. Update each AI call site

Example for `suggest-topics`:

```python
@ai_bp.route('/api/ai/suggest-topics', methods=['POST'])
def api_ai_suggest_topics():
    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()

    if not call_notes or len(call_notes) < 10:
        return jsonify({'success': False, 'error': 'Call notes are too short'}), 400

    try:
        if get_ai_mode() == 'gateway':
            from app.services.ai_gateway import call_gateway
            result = call_gateway('/v1/suggest-topics', {'call_notes': call_notes})
            suggested_topics = result.get('topics', [])
            # Log locally (existing AIQueryLog pattern)
            # ... same logging as today ...
        else:
            # Existing direct OpenAI code (unchanged)
            # ...
            pass

        # Process topics (existing logic — check/create in DB)
        # ...
    except Exception as e:
        # Error logging (existing pattern)
        # ...
```

### 4. Files to modify

| File | Changes |
|---|---|
| `app/services/ai_gateway.py` | **New file** — gateway HTTP client |
| `app/routes/ai.py` | Add `get_ai_mode()`, update `is_ai_enabled()`, add gateway path to all 4 route functions |
| `app/routes/connect_export.py` | Update `_call_openai()` to use gateway when `AI_MODE=gateway` |
| `app/routes/admin.py` | Update admin panel to show gateway mode, simplify AI config card |
| `templates/admin_panel.html` | Show 'Gateway mode' vs 'Direct mode', hide env var cards in gateway mode |
| `.env.example` | Add `AI_MODE=gateway` (default), keep existing vars for direct mode |
| `README.md` | Update AI Features section — gateway is now the default, direct is for dev/offline |
| `tests/test_ai.py` | Add tests for gateway mode (mock `call_gateway`) |

### 5. Updated `.env.example` additions

```dotenv
# AI Mode: 'gateway' (centralized, no setup needed) or 'direct' (bring your own OpenAI)
AI_MODE=gateway

# Direct mode only — not needed for gateway mode:
# AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
# AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
# AZURE_OPENAI_API_VERSION=2024-08-01-preview
# AZURE_CLIENT_ID=your-app-id
# AZURE_CLIENT_SECRET=your-password
# AZURE_TENANT_ID=your-tenant-id
```

---

## Security Model

### Authentication flow

1. User signs into NoteHelper with their Microsoft Entra ID account (existing flow).
2. When an AI feature is triggered, NoteHelper acquires an access token for the gateway audience (`api://notehelper-ai-gateway/access`) using MSAL's on-behalf-of or silent token acquisition.
3. NoteHelper sends the request to the APIM endpoint with `Authorization: Bearer <token>`.
4. APIM validates the JWT:
   - Token is issued by Microsoft Entra ID
   - Audience matches `api://notehelper-ai-gateway`
   - Tenant ID (`tid` claim) is in the allowlist (Microsoft corporate tenant)
   - Token is not expired
5. APIM routes to the Function App.
6. Function App builds the prompt and calls Azure OpenAI with its own Managed Identity.

### What's protected against

| Threat | Mitigation |
|---|---|
| Non-Microsoft user | JWT `tid` claim validation |
| Using endpoint as general GPT proxy | Prompts hardcoded server-side; endpoints accept only structured data |
| Abuse / excessive usage | APIM per-user rate limit (20/min) + daily quota (200/day) |
| Stolen JWT | Short token lifetime (typically 1 hour), per-user quota limits blast radius |
| Direct access to Function App | Function App requires function key (only APIM has it) |
| OpenAI key exposure | Key in Key Vault, accessed via Managed Identity only |

---

## Quota & Rate Limiting

All handled by APIM policies (no external state store needed).

| Scope | Limit | Key | Renewal |
|---|---|---|---|
| Per-user, per-minute | 20 calls | `oid` claim | 60 seconds |
| Per-user, per-day | 200 calls | `oid` claim | 86400 seconds |
| Connect-summary, per-user, per-day | 10 calls | `oid` + endpoint | 86400 seconds |

These limits are starting points. Adjust based on actual usage patterns once deployed.

---

## Telemetry

### What's logged where

| Layer | What's logged | Where |
|---|---|---|
| **APIM diagnostics** | Request count, latency, status codes, user `oid` (hashed), endpoint name | Existing Application Insights |
| **Function App** | Errors, OpenAI response times, token counts | Existing Application Insights (via Function App config) |
| **NoteHelper local** | `AIQueryLog` table entries (request summary, response summary, success/fail, tokens) | Local SQLite DB (unchanged) |

### What's NOT logged

- Prompt text (system or user) — not sent to App Insights
- Call note content — stays in NoteHelper's local DB only
- Customer names or PII — not in gateway telemetry

### New: Gateway error logging in NoteHelper

When APIM returns an error (401, 429, 500) that wouldn't normally reach `AIQueryLog`, NoteHelper catches it and writes a log entry:

```python
log_entry = AIQueryLog(
    request_text=f"Gateway call to {endpoint}",
    response_text=None,
    success=False,
    error_message=f"Gateway returned {status_code}: {error_body}"
)
```

---

## Future: Token-Based Quotas

The Function App architecture enables token-budget enforcement without any client changes.

### How it would work

1. Add Azure Table Storage or Cosmos DB (serverless) to the resource group.
2. After each OpenAI call, the Function writes a row:
   ```
   | PartitionKey (user_oid) | RowKey (timestamp) | endpoint | prompt_tokens | completion_tokens |
   ```
3. Before each call, the Function queries today's total for the user.
4. If `SUM(total_tokens) > daily_budget`, return 429 with a clear error message.

### Why not now

- APIM call-count quotas are sufficient for launch.
- Token tracking adds latency (Table Storage read before each call).
- We don't yet have usage data to set meaningful token budgets.

### When to add it

- If a small number of users consume disproportionate tokens (e.g., heavy connect-summary usage).
- If OpenAI costs exceed expectations and call-count limits aren't granular enough.

---

## Checklist

### Phase 1A — Azure Resources
- [ ] Create resource group `rg-notehelper-ai-gateway`
- [ ] Create Key Vault, store OpenAI key
- [ ] Create Function App with Managed Identity
- [ ] Grant MI → Key Vault (GET secrets) + Azure OpenAI (Cognitive Services OpenAI User)
- [ ] Register Entra ID app `NoteHelper AI Gateway` with audience URI
- [ ] Create APIM instance (Consumption)
- [ ] Wire APIM diagnostics → Application Insights

### Phase 1B — Function App
- [ ] Create Function App project (Python)
- [ ] Implement shared OpenAI client module
- [ ] Copy prompt templates from NoteHelper
- [ ] Implement all 6 HTTP trigger functions
- [ ] Deploy and test each endpoint individually

### Phase 1C — APIM
- [ ] Import Function App as APIM backend
- [ ] Configure JWT validation policy (Microsoft tenant)
- [ ] Configure rate-limit and quota policies
- [ ] Add per-endpoint overrides (connect-summary)
- [ ] Test end-to-end with curl + valid JWT

### Phase 2 — NoteHelper Code
- [ ] Create `app/services/ai_gateway.py`
- [ ] Add `AI_MODE` config switch
- [ ] Update `suggest-topics` route
- [ ] Update `match-milestone` route
- [ ] Update `analyze-call` route
- [ ] Update `generate-engagement-summary` route
- [ ] Update `_call_openai()` in connect_export.py
- [ ] Update admin panel (gateway mode display)
- [ ] Update `.env.example` and README
- [ ] Write tests for gateway mode
- [ ] Test direct mode still works (backward compatibility)