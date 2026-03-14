"""
NoteHelper AI Gateway — Flask application.

A thin, purpose-built proxy between NoteHelper and Azure OpenAI.
Each endpoint accepts structured input, constructs the prompt server-side,
calls OpenAI via Managed Identity, and returns narrow JSON.

Deployed on Azure App Service (B1).
APIM sits in front and handles JWT validation + rate limiting.
"""
import json
import logging
import os
import re

from flask import Flask, request, jsonify
from flask_socketio import SocketIO

from openai_client import chat_completion, get_connect_deployment
from prompts import (
    TOPIC_SUGGESTION_PROMPT,
    MILESTONE_MATCH_PROMPT,
    ANALYZE_CALL_PROMPT,
    ENGAGEMENT_SUMMARY_PROMPT,
    ENGAGEMENT_STORY_PROMPT,
    MILESTONE_COMMENT_PROMPT,
    CONNECT_SUMMARY_SYSTEM_PROMPT,
    CONNECT_CHUNK_SYSTEM_PROMPT,
    CONNECT_SYNTHESIS_SYSTEM_PROMPT,
    CONNECT_USER_PROMPT_SINGLE,
    CONNECT_USER_PROMPT_CHUNK,
    CONNECT_USER_PROMPT_SYNTHESIS,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Socket.IO — allow cross-origin from NoteHelper local instances
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Register the partner sharing namespace
from sharing_hub import ShareNamespace
socketio.on_namespace(ShareNamespace("/share"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gateway")

# Wire up Application Insights if connection string is set
_ai_conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _ai_conn:
    try:
        from opencensus.ext.azure.log_exporter import AzureLogHandler

        handler = AzureLogHandler(connection_string=_ai_conn)
        logging.getLogger().addHandler(handler)
        logger.info("Application Insights telemetry enabled")
    except ImportError:
        logger.warning(
            "opencensus-ext-azure not installed — App Insights disabled"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_json_array(text: str) -> list:
    """Extract a JSON array from potentially messy LLM output."""
    clean = text
    if "```" in clean:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
        if match:
            clean = match.group(1).strip()
    array_match = re.search(r"\[.*\]", clean, re.DOTALL)
    if array_match:
        clean = array_match.group(0)
    return json.loads(clean)


def _parse_json_object(text: str) -> dict:
    """Extract a JSON object from potentially messy LLM output."""
    clean = text
    if "```" in clean:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
        if match:
            clean = match.group(1).strip()
    obj_match = re.search(r"\{.*\}", clean, re.DOTALL)
    if obj_match:
        clean = obj_match.group(0)
    return json.loads(clean)


def _error(msg: str, status: int = 400):
    """Return a standard error response."""
    return jsonify({"success": False, "error": msg}), status


# ---------------------------------------------------------------------------
# APIM Gateway Secret validation
# ---------------------------------------------------------------------------
_GATEWAY_SECRET = os.environ.get("APIM_GATEWAY_SECRET")


@app.before_request
def _validate_gateway_secret():
    """Reject requests that don't have the correct APIM gateway secret.

    This ensures only traffic through APIM reaches the gateway.
    The secret is injected by APIM policy as X-Gateway-Secret header.
    """
    # Skip validation if no secret is configured (dev mode)
    if not _GATEWAY_SECRET:
        return None

    # Allow health checks without auth
    if request.path == "/health":
        return None

    incoming_secret = request.headers.get("X-Gateway-Secret")
    if incoming_secret != _GATEWAY_SECRET:
        logger.warning(
            "Request rejected: invalid or missing X-Gateway-Secret header"
        )
        return _error("Unauthorized: invalid gateway secret", 403)

    return None


# ---------------------------------------------------------------------------
# POST /v1/suggest-topics
# ---------------------------------------------------------------------------
@app.route("/v1/suggest-topics", methods=["POST"])
def suggest_topics():
    """Suggest topic tags from call notes."""
    try:
        body = request.get_json(force=True)
        call_notes = (body.get("call_notes") or "").strip()

        if not call_notes or len(call_notes) < 10:
            return _error("call_notes is required (min 10 chars)")

        result = chat_completion(
            TOPIC_SUGGESTION_PROMPT,
            f"Call notes:\n\n{call_notes}",
            max_tokens=150,
        )

        topics = _parse_json_array(result["text"])
        topics = [str(t).strip() for t in topics if t and str(t).strip()]

        return jsonify({
            "success": True,
            "topics": topics,
            "usage": result["usage"],
        })

    except json.JSONDecodeError:
        logger.warning("suggest-topics: could not parse AI response as JSON")
        return _error("AI returned invalid response format", 502)
    except Exception as exc:
        logger.exception("suggest-topics error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# Milestone status tiers for prioritised matching
# ---------------------------------------------------------------------------
_MILESTONE_STATUS_TIERS = [
    {"On Track"},
    {"At Risk"},
    {"Blocked"},
]


def _try_match_milestones(call_notes: str, milestones: list) -> dict | None:
    """Attempt to AI-match call notes against a list of milestones.

    Returns the parsed result dict if a match was found, or None.
    """
    milestone_list = "\n".join([
        f"- ID: {m.get('id')}, Name: {m.get('name')}, "
        f"Status: {m.get('status')}, "
        f"Opportunity: {m.get('opportunity', '')}, "
        f"Workload: {m.get('workload', '')}"
        for m in milestones
    ])
    user_prompt = (
        f"Call Notes:\n{call_notes[:2000]}\n\n"
        f"Available Milestones:\n{milestone_list}\n\n"
        "Which milestone best matches what was discussed in the call?"
    )

    result = chat_completion(
        MILESTONE_MATCH_PROMPT, user_prompt, max_tokens=150,
    )
    parsed = _parse_json_object(result["text"])
    if parsed.get("milestone_id"):
        return {
            "success": True,
            "milestone_id": parsed["milestone_id"],
            "reason": parsed.get("reason", ""),
            "usage": result["usage"],
        }
    return None


# ---------------------------------------------------------------------------
# POST /v1/match-milestone
# ---------------------------------------------------------------------------
@app.route("/v1/match-milestone", methods=["POST"])
def match_milestone():
    """Match call notes to the best milestone, preferring active statuses.

    Tries milestones in priority order: On Track → At Risk → Blocked → rest.
    Stops on the first tier that produces a match.
    """
    try:
        body = request.get_json(force=True)
        call_notes = (body.get("call_notes") or "").strip()
        milestones = body.get("milestones") or []

        if not call_notes or len(call_notes) < 20:
            return _error("call_notes is required (min 20 chars)")
        if not milestones:
            return _error("milestones list is required")

        # Build status tiers + a catch-all for remaining statuses
        active_statuses = set().union(*_MILESTONE_STATUS_TIERS)
        tiers = [
            [m for m in milestones if m.get("status") in tier]
            for tier in _MILESTONE_STATUS_TIERS
        ]
        tiers.append([m for m in milestones if m.get("status") not in active_statuses])

        aggregated_usage: dict = {}

        for tier_milestones in tiers:
            if not tier_milestones:
                continue
            try:
                match = _try_match_milestones(call_notes, tier_milestones)
                if match:
                    return jsonify(match)
                # Track usage even on non-match (last call's usage is fine)
            except json.JSONDecodeError:
                logger.warning("match-milestone: bad JSON from AI in tier")
                continue

        # No match in any tier
        return jsonify({
            "success": True,
            "milestone_id": None,
            "reason": "No milestone matches the call discussion",
            "usage": aggregated_usage,
        })

    except Exception as exc:
        logger.exception("match-milestone error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/analyze-call
# ---------------------------------------------------------------------------
@app.route("/v1/analyze-call", methods=["POST"])
def analyze_call():
    """Extract topic tags from call notes (auto-fill flow)."""
    try:
        body = request.get_json(force=True)
        call_notes = (body.get("call_notes") or "").strip()

        if not call_notes or len(call_notes) < 20:
            return _error("call_notes is required (min 20 chars)")

        user_prompt = (
            "Analyze these call notes and extract the key "
            f"topics/technologies discussed:\n\n{call_notes[:3000]}"
        )
        result = chat_completion(
            ANALYZE_CALL_PROMPT, user_prompt, max_tokens=200,
        )
        parsed = _parse_json_object(result["text"])

        return jsonify({
            "success": True,
            "topics": parsed.get("topics", []),
            "usage": result["usage"],
        })

    except json.JSONDecodeError:
        logger.warning("analyze-call: could not parse AI response as JSON")
        return _error("AI returned invalid response format", 502)
    except Exception as exc:
        logger.exception("analyze-call error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/engagement-summary
# ---------------------------------------------------------------------------
@app.route("/v1/engagement-summary", methods=["POST"])
def engagement_summary():
    """Generate a structured engagement summary from customer call logs."""
    try:
        body = request.get_json(force=True)
        customer_name = body.get("customer_name", "")
        tpid = body.get("tpid", "")
        overview = body.get("overview", "")
        notes = body.get("notes") or []

        if not notes:
            return _error("notes list is required")

        # Build call text (mirrors app/routes/ai.py logic)
        parts = []
        for n in notes:
            entry = f"[{n.get('date', '')}]"
            topics = n.get("topics", [])
            if topics:
                entry += f" Topics: {', '.join(topics)}"
            entry += f"\n{n.get('content', '')}"
            parts.append(entry)
        call_text = "\n\n---\n\n".join(parts)

        MAX_CHARS = 30_000
        if len(call_text) > MAX_CHARS:
            call_text = (
                call_text[:MAX_CHARS]
                + "\n\n[... additional notes truncated ...]"
            )

        notes_section = (
            f"\nExisting Customer Notes:\n{overview}\n" if overview else ""
        )
        user_message = (
            f"Customer: {customer_name} (TPID: {tpid})\n"
            f"Total notes: {len(notes)}\n"
            f"{notes_section}\n"
            f"Notes:\n\n{call_text}"
        )

        result = chat_completion(
            ENGAGEMENT_SUMMARY_PROMPT, user_message, max_tokens=1000,
        )

        return jsonify({
            "success": True,
            "summary": result["text"],
            "usage": result["usage"],
        })

    except Exception as exc:
        logger.exception("engagement-summary error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/engagement-story
# ---------------------------------------------------------------------------
@app.route("/v1/engagement-story", methods=["POST"])
def engagement_story():
    """Generate structured story fields for a customer engagement."""
    try:
        body = request.get_json(force=True)
        user_message = (body.get("user_message") or "").strip()

        if not user_message:
            return _error("user_message is required")

        result = chat_completion(
            ENGAGEMENT_STORY_PROMPT, user_message, max_tokens=1000,
        )

        # Parse JSON from response
        parsed = _parse_json_object(result["text"])

        return jsonify({
            "success": True,
            "story": parsed,
            "usage": result["usage"],
        })

    except json.JSONDecodeError:
        logger.warning("engagement-story: could not parse AI response as JSON")
        return _error("AI returned invalid response format", 502)
    except Exception as exc:
        logger.exception("engagement-story error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/summarize-note
# ---------------------------------------------------------------------------
@app.route("/v1/summarize-note", methods=["POST"])
def summarize_note():
    """Summarize a call log for a milestone comment.

    Accepts the call log text and a list of existing milestone comments.
    Returns a 2-4 sentence summary covering only new information, or
    ``NO_NEW_INFO`` if the call adds nothing beyond what's already tracked.
    """
    try:
        body = request.get_json(force=True)
        call_notes = (body.get("call_notes") or "").strip()
        existing_comments = body.get("existing_comments") or []
        customer_name = body.get("customer_name", "")
        topics = body.get("topics", "")

        if not call_notes or len(call_notes) < 20:
            return _error("call_notes is required (min 20 chars)")

        # Build context section from existing milestone comments
        if existing_comments:
            existing_section = "\n\n".join(
                f"--- Existing comment {i + 1} ---\n{c}"
                for i, c in enumerate(existing_comments)
            )
        else:
            existing_section = "(No existing comments on this milestone.)"

        user_prompt = (
            f"Customer: {customer_name}\n"
            f"Topics: {topics}\n\n"
            f"=== EXISTING MILESTONE COMMENTS ===\n{existing_section}\n\n"
            f"=== NEW CALL LOG ===\n{call_notes[:10000]}"
        )

        result = chat_completion(
            MILESTONE_COMMENT_PROMPT, user_prompt, max_tokens=300,
        )

        summary = result["text"].strip()

        return jsonify({
            "success": True,
            "summary": summary,
            "no_new_info": summary == "NO_NEW_INFO",
            "usage": result["usage"],
        })

    except Exception as exc:
        logger.exception("summarize-note error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/connect-summary
# ---------------------------------------------------------------------------
@app.route("/v1/connect-summary", methods=["POST"])
def connect_summary():
    """Generate Connect self-evaluation narrative using GPT-5.3-chat.

    Uses the evidence scaffolding prompt pattern for better synthesis.

    Supports three modes:
      - single:    Full export → single summary
      - chunk:     Per-customer-group evidence extraction
      - synthesis: Combine chunk evidence into final output
    """
    try:
        body = request.get_json(force=True)
        mode = body.get("mode", "single")
        deployment = get_connect_deployment()

        if mode == "single":
            text_export = body.get("text_export", "")
            user_prompt = CONNECT_USER_PROMPT_SINGLE.format(
                text_export=text_export,
            )
            result = chat_completion(
                CONNECT_SUMMARY_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=3000,
                deployment=deployment,
                temperature=0.2,
            )

        elif mode == "chunk":
            header = body.get("header", "")
            customer_text = body.get("customer_text", "")
            general_notes_text = body.get("general_notes_text", "")
            chunk_index = body.get("chunk_index", 1)
            chunk_count = body.get("chunk_count", 1)
            user_prompt = CONNECT_USER_PROMPT_CHUNK.format(
                header=header,
                customer_text=customer_text,
                general_notes_text=general_notes_text,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
            )
            result = chat_completion(
                CONNECT_CHUNK_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=2000,
                deployment=deployment,
                temperature=0.2,
            )

        elif mode == "synthesis":
            header = body.get("header", "")
            partial_summaries = body.get("partial_summaries", [])
            chunk_count = body.get("chunk_count", len(partial_summaries))
            combined = "\n\n---\n\n".join(
                f"### Chunk {i + 1}\n{s}"
                for i, s in enumerate(partial_summaries)
            )
            user_prompt = CONNECT_USER_PROMPT_SYNTHESIS.format(
                header=header,
                chunk_count=chunk_count,
                combined=combined,
            )
            result = chat_completion(
                CONNECT_SYNTHESIS_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=3000,
                deployment=deployment,
                temperature=0.2,
            )

        else:
            return _error(f"Invalid mode: {mode}")

        return jsonify({
            "success": True,
            "summary": result["text"],
            "usage": result["usage"],
        })

    except Exception as exc:
        logger.exception("connect-summary error")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# POST /v1/ping
# ---------------------------------------------------------------------------
@app.route("/v1/ping", methods=["POST"])
def ping():
    """Health check — verifies the gateway can reach Azure OpenAI."""
    try:
        result = chat_completion(
            "You are a helpful assistant.",
            "Say 'Connection successful!' and nothing else.",
            max_tokens=20,
        )
        return jsonify({
            "success": True,
            "status": "ok",
            "response": result["text"],
        })
    except Exception as exc:
        logger.exception("ping error")
        return _error(f"OpenAI unreachable: {exc}", 502)


# ---------------------------------------------------------------------------
# GET / — basic liveness probe
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    """Liveness probe for App Service (no OpenAI call)."""
    return jsonify({"status": "ok", "service": "notehelper-ai-gateway"})


# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
