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

from openai_client import chat_completion
from prompts import (
    TOPIC_SUGGESTION_PROMPT,
    MILESTONE_MATCH_PROMPT,
    ANALYZE_CALL_PROMPT,
    ENGAGEMENT_SUMMARY_PROMPT,
    ENGAGEMENT_STORY_PROMPT,
    CONNECT_SUMMARY_SYSTEM_PROMPT,
    CONNECT_CHUNK_SYSTEM_PROMPT,
    CONNECT_SYNTHESIS_SYSTEM_PROMPT,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

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
# POST /v1/match-milestone
# ---------------------------------------------------------------------------
@app.route("/v1/match-milestone", methods=["POST"])
def match_milestone():
    """Match call notes to the best milestone from a provided list."""
    try:
        body = request.get_json(force=True)
        call_notes = (body.get("call_notes") or "").strip()
        milestones = body.get("milestones") or []

        if not call_notes or len(call_notes) < 20:
            return _error("call_notes is required (min 20 chars)")
        if not milestones:
            return _error("milestones list is required")

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

        return jsonify({
            "success": True,
            "milestone_id": parsed.get("milestone_id"),
            "reason": parsed.get("reason", ""),
            "usage": result["usage"],
        })

    except json.JSONDecodeError:
        logger.warning("match-milestone: could not parse AI response as JSON")
        return _error("AI returned invalid response format", 502)
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
# POST /v1/connect-summary
# ---------------------------------------------------------------------------
@app.route("/v1/connect-summary", methods=["POST"])
def connect_summary():
    """Generate Connect self-evaluation narrative.

    Supports three modes:
      - single:    Full export → single summary
      - chunk:     Per-customer-group partial summary
      - synthesis: Combine chunk summaries into final output
    """
    try:
        body = request.get_json(force=True)
        mode = body.get("mode", "single")

        if mode == "single":
            text_export = body.get("text_export", "")
            user_prompt = (
                "Here is my note data for this Connect period.  "
                "Please write my Connect self-evaluation.\n\n"
                f"{text_export}"
            )
            result = chat_completion(
                CONNECT_SUMMARY_SYSTEM_PROMPT, user_prompt, max_tokens=2000,
            )

        elif mode == "chunk":
            header = body.get("header", "")
            customer_text = body.get("customer_text", "")
            general_notes_text = body.get("general_notes_text", "")
            chunk_index = body.get("chunk_index", 1)
            chunk_count = body.get("chunk_count", 1)
            user_prompt = (
                f"Overall period stats:\n{header}\n\n"
                f"Customer details (chunk {chunk_index} of {chunk_count}):\n\n"
                f"{customer_text}{general_notes_text}"
            )
            result = chat_completion(
                CONNECT_CHUNK_SYSTEM_PROMPT, user_prompt, max_tokens=1500,
            )

        elif mode == "synthesis":
            header = body.get("header", "")
            partial_summaries = body.get("partial_summaries", [])
            chunk_count = body.get("chunk_count", len(partial_summaries))
            combined = "\n\n---\n\n".join(
                f"### Chunk {i + 1}\n{s}"
                for i, s in enumerate(partial_summaries)
            )
            user_prompt = (
                f"Overall period stats:\n{header}\n\n"
                f"Here are partial summaries from {chunk_count} "
                f"customer groups:\n\n{combined}"
            )
            result = chat_completion(
                CONNECT_SYNTHESIS_SYSTEM_PROMPT, user_prompt, max_tokens=2000,
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
def health():
    """Liveness probe for App Service (no OpenAI call)."""
    return jsonify({"status": "ok", "service": "notehelper-ai-gateway"})


# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
