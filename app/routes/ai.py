"""
AI routes for NoteHelper.
Handles AI-powered topic suggestion and related features.

All AI calls go through the APIM gateway by default
(NoteHelper → APIM → App Service gateway → Azure OpenAI).
A direct-to-OpenAI legacy path is preserved for fallback when
``AZURE_OPENAI_ENDPOINT`` + ``AZURE_OPENAI_DEPLOYMENT`` are set and
the gateway is explicitly bypassed.
"""
from flask import Blueprint, request, jsonify, g
from datetime import date
import json
import logging
import os

from app.models import db, AIQueryLog, Topic
from app.gateway_client import is_gateway_enabled, gateway_call, GatewayError

logger = logging.getLogger(__name__)

# Create blueprint
ai_bp = Blueprint('ai', __name__)

# System prompt for topic suggestion (used only in direct/legacy mode)
TOPIC_SUGGESTION_PROMPT = (
    "You are a helpful assistant that analyzes call notes and suggests relevant topic tags. "
    "Based on the call notes provided, return a JSON array of 3-7 short topic tags (1-3 words each) "
    "that best describe the key technologies, products, or themes discussed. "
    "Return ONLY a JSON array of strings, nothing else. "
    'Example: ["Azure OpenAI", "Vector Search", "RAG Pattern"]'
)


def is_ai_enabled() -> bool:
    """Check if AI features are enabled.

    Returns True when *either* the APIM gateway or direct Azure OpenAI
    credentials are configured.
    """
    if is_gateway_enabled():
        return True
    return bool(
        os.environ.get('AZURE_OPENAI_ENDPOINT')
        and os.environ.get('AZURE_OPENAI_DEPLOYMENT')
    )


def is_gateway_mode() -> bool:
    """Return True if calls should go through the APIM gateway."""
    return is_gateway_enabled()


def get_openai_deployment() -> str:
    """Get the Azure OpenAI deployment name from environment."""
    return os.environ.get('AZURE_OPENAI_DEPLOYMENT', '')


def get_azure_openai_client():
    """Create an Azure OpenAI client with Entra ID authentication.
    
    All configuration is read from environment variables:
    - AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID (service principal)
    - AZURE_OPENAI_ENDPOINT (endpoint URL)
    - AZURE_OPENAI_API_VERSION (optional, defaults to 2024-08-01-preview)
    """
    from openai import AzureOpenAI
    from azure.identity import ClientSecretCredential, get_bearer_token_provider
    
    # Get service principal credentials from environment
    client_id = os.environ.get('AZURE_CLIENT_ID')
    client_secret = os.environ.get('AZURE_CLIENT_SECRET')
    tenant_id = os.environ.get('AZURE_TENANT_ID')
    
    if not all([client_id, client_secret, tenant_id]):
        raise ValueError("Missing Azure service principal environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)")
    
    # Get OpenAI endpoint from environment
    endpoint_url = os.environ.get('AZURE_OPENAI_ENDPOINT')
    api_version = os.environ.get('AZURE_OPENAI_API_VERSION', '2024-08-01-preview')
    
    if not endpoint_url:
        raise ValueError("Missing AZURE_OPENAI_ENDPOINT environment variable")
    
    # Create credential and token provider
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret
    )
    token_provider = get_bearer_token_provider(
        credential, 
        "https://cognitiveservices.azure.com/.default"
    )
    
    # Create Azure OpenAI client
    client = AzureOpenAI(
        api_version=api_version,
        azure_endpoint=endpoint_url,
        azure_ad_token_provider=token_provider,
    )
    
    return client


@ai_bp.route('/api/ai/suggest-topics', methods=['POST'])
def api_ai_suggest_topics():
    """Generate topic suggestions from call notes using AI."""

    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()

    if not call_notes or len(call_notes) < 10:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400

    try:
        # ---- Gateway path ----
        if is_gateway_mode():
            result = gateway_call("/v1/suggest-topics", {"call_notes": call_notes})
            suggested_topics = result.get("topics", [])
            usage = result.get("usage", {})

            log_entry = AIQueryLog(
                request_text=call_notes[:1000],
                response_text=json.dumps(suggested_topics)[:1000],
                success=True,
                model=usage.get("model", "gateway"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
            db.session.add(log_entry)

        # ---- Direct / legacy path ----
        else:
            deployment_name = get_openai_deployment()
            client = get_azure_openai_client()

            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": TOPIC_SUGGESTION_PROMPT},
                    {"role": "user", "content": f"Call notes:\n\n{call_notes}"}
                ],
                max_tokens=150,
                model=deployment_name
            )

            response_text = response.choices[0].message.content
            if not response_text or not response_text.strip():
                raise ValueError("AI returned empty content")
            response_text = response_text.strip()
            raw_response_text = response_text

            model_used = response.model or deployment_name
            prompt_tokens = response.usage.prompt_tokens if response.usage else None
            completion_tokens = response.usage.completion_tokens if response.usage else None
            total_tokens = response.usage.total_tokens if response.usage else None

            # Parse JSON response
            import re
            clean_text = response_text
            if '```' in clean_text:
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', clean_text, re.DOTALL)
                if match:
                    clean_text = match.group(1).strip()
                else:
                    clean_text = clean_text.replace('```json', '').replace('```', '').strip()
            array_match = re.search(r'\[.*\]', clean_text, re.DOTALL)
            if array_match:
                clean_text = array_match.group(0)

            suggested_topics = json.loads(clean_text)
            if not isinstance(suggested_topics, list):
                raise ValueError("Response is not a list")
            suggested_topics = [str(t).strip() for t in suggested_topics if t and str(t).strip()]
            if not suggested_topics:
                raise ValueError("No topics returned")

            log_entry = AIQueryLog(
                request_text=call_notes[:1000],
                response_text=raw_response_text[:1000],
                success=True,
                model=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
            db.session.add(log_entry)

        # ---- Common: create / match topics in DB ----
        topic_ids = []
        for topic_name in suggested_topics:
            existing_topic = Topic.query.filter(
                db.func.lower(Topic.name) == topic_name.lower()
            ).first()
            if existing_topic:
                topic_ids.append({'id': existing_topic.id, 'name': existing_topic.name})
            else:
                new_topic = Topic(name=topic_name)
                db.session.add(new_topic)
                db.session.flush()
                topic_ids.append({'id': new_topic.id, 'name': new_topic.name})

        db.session.commit()
        return jsonify({'success': True, 'topics': topic_ids})

    except GatewayError as e:
        log_entry = AIQueryLog(
            request_text=call_notes[:1000], response_text=None,
            success=False, error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500

    except (json.JSONDecodeError, ValueError) as e:
        log_entry = AIQueryLog(
            request_text=call_notes[:1000],
            response_text=(raw_response_text[:1000] if 'raw_response_text' in dir() else None),
            success=False, error_message=f"Parse error: {str(e)}"
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': 'AI returned invalid response format'}), 500

    except Exception as e:
        log_entry = AIQueryLog(
            request_text=call_notes[:1000], response_text=None,
            success=False, error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500


@ai_bp.route('/api/ai/match-milestone', methods=['POST'])
def api_ai_match_milestone():
    """Match call notes to the most relevant milestone using AI."""

    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()
    milestones = data.get('milestones', [])

    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400
    if not milestones or len(milestones) == 0:
        return jsonify({'success': False, 'error': 'No milestones provided'}), 400

    try:
        # ---- Gateway path ----
        if is_gateway_mode():
            result = gateway_call("/v1/match-milestone", {
                "call_notes": call_notes,
                "milestones": milestones,
            })
            log_entry = AIQueryLog(
                request_text=f"Match milestone: {call_notes[:500]}...",
                response_text=json.dumps(result)[:500],
                success=True,
            )
            db.session.add(log_entry)
            db.session.commit()

            return jsonify({
                'success': True,
                'matched_milestone_id': result.get('milestone_id'),
                'reason': result.get('reason', '')
            })

        # ---- Direct / legacy path ----
        deployment_name = get_openai_deployment()
        milestone_list = "\n".join([
            f"- ID: {m.get('id')}, Name: {m.get('name')}, Status: {m.get('status')}, Opportunity: {m.get('opportunity', '')}, Workload: {m.get('workload', '')}"
            for m in milestones
        ])

        system_prompt = """You are an expert at matching customer call notes to sales milestones.
Your task is to identify which milestone best matches the topics discussed in the call notes.

Respond with ONLY a JSON object in this exact format (no markdown, no explanation):
{"milestone_id": "THE_MATCHED_ID", "reason": "Brief explanation of why this milestone matches"}

If no milestone is a good match, respond with:
{"milestone_id": null, "reason": "No milestone matches the call discussion"}"""

        user_prompt = f"""Call Notes:
{call_notes[:2000]}

Available Milestones:
{milestone_list}

Which milestone best matches what was discussed in the call?"""

        client = get_azure_openai_client()
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=150,
            model=deployment_name
        )
        response_text = response.choices[0].message.content.strip()

        log_entry = AIQueryLog(
            request_text=f"Match milestone: {call_notes[:500]}...",
            response_text=response_text[:500],
            success=True
        )
        db.session.add(log_entry)
        db.session.commit()

        import re
        clean_text = response_text
        if '```' in clean_text:
            match = re.search(r'```(?:json)?\s*(.*?)\s*```', clean_text, re.DOTALL)
            if match:
                clean_text = match.group(1).strip()

        result = json.loads(clean_text)
        return jsonify({
            'success': True,
            'matched_milestone_id': result.get('milestone_id'),
            'reason': result.get('reason', '')
        })

    except (GatewayError, json.JSONDecodeError) as e:
        log_entry = AIQueryLog(
            request_text=f"Match milestone: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500

    except Exception as e:
        log_entry = AIQueryLog(
            request_text=f"Match milestone: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500


@ai_bp.route('/api/ai/analyze-call', methods=['POST'])
def api_ai_analyze_call():
    """
    Analyze call notes to extract and auto-tag topics.
    This is the AI call in the auto-fill flow for topic matching.
    Task title/description come from WorkIQ, not OpenAI.
    
    Takes: call_notes (str)
    Returns: topics (list of {id, name})
    """

    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()

    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400

    try:
        # ---- Gateway path ----
        if is_gateway_mode():
            result = gateway_call("/v1/analyze-call", {"call_notes": call_notes})
            topics = result.get("topics", [])
            usage = result.get("usage", {})

            log_entry = AIQueryLog(
                request_text=f"Analyze call: {call_notes[:500]}...",
                response_text=json.dumps(topics)[:500],
                success=True,
                model=usage.get("model", "gateway"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
            db.session.add(log_entry)

        # ---- Direct / legacy path ----
        else:
            deployment_name = get_openai_deployment()

            system_prompt = """You are an expert at analyzing Azure customer call notes.
Extract the key technologies and concepts discussed.

Respond with ONLY a JSON object in this exact format (no markdown, no explanation):
{
  "topics": ["Topic 1", "Topic 2", "Topic 3"]
}

Guidelines:
- topics: List 2-5 Azure/Microsoft technologies or concepts discussed (e.g., "Azure Kubernetes Service", "Cost Optimization", "Data Migration")
- Focus on specific, actionable technology areas rather than generic terms"""

            user_prompt = f"""Analyze these call notes and extract the key topics/technologies discussed:

{call_notes[:3000]}"""

            client = get_azure_openai_client()
            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=200,
                model=deployment_name
            )
            response_text = response.choices[0].message.content.strip()

            model_used = response.model or deployment_name
            prompt_tokens = response.usage.prompt_tokens if response.usage else None
            completion_tokens = response.usage.completion_tokens if response.usage else None
            total_tokens = response.usage.total_tokens if response.usage else None

            import re
            clean_text = response_text
            if '```' in clean_text:
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', clean_text, re.DOTALL)
                if match:
                    clean_text = match.group(1).strip()

            parsed = json.loads(clean_text)
            topics = parsed.get('topics', [])

            log_entry = AIQueryLog(
                request_text=f"Analyze call: {call_notes[:500]}...",
                response_text=response_text[:500],
                success=True,
                model=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
            db.session.add(log_entry)

        # ---- Common: create / match topics in DB ----
        topic_ids = []
        for topic_name in topics:
            if not topic_name or not str(topic_name).strip():
                continue
            topic_name = str(topic_name).strip()

            existing_topic = Topic.query.filter(
                db.func.lower(Topic.name) == topic_name.lower()
            ).first()

            if existing_topic:
                topic_ids.append({'id': existing_topic.id, 'name': existing_topic.name})
            else:
                new_topic = Topic(name=topic_name)
                db.session.add(new_topic)
                db.session.flush()
                topic_ids.append({'id': new_topic.id, 'name': new_topic.name})

        db.session.commit()
        return jsonify({'success': True, 'topics': topic_ids})

    except (GatewayError, json.JSONDecodeError) as e:
        log_entry = AIQueryLog(
            request_text=f"Analyze call: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500

    except Exception as e:
        log_entry = AIQueryLog(
            request_text=f"Analyze call: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500


# System prompt for customer engagement summary generation
ENGAGEMENT_SUMMARY_PROMPT = (
    "You are a Microsoft technical seller's assistant. Analyze the provided notes "
    "and any existing customer account context for a customer and generate a structured engagement summary. "
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


@ai_bp.route('/api/ai/generate-engagement-summary', methods=['POST'])
def api_ai_generate_engagement_summary():
    """Generate a structured engagement summary from all call logs for a customer."""
    if not is_ai_enabled():
        return jsonify({
            'success': False,
            'error': 'AI features are not configured'
        }), 400

    data = request.get_json()
    customer_id = data.get('customer_id') if data else None
    if not customer_id:
        return jsonify({'success': False, 'error': 'customer_id is required'}), 400

    from app.models import Customer, Note
    customer = Customer.query.get(customer_id)
    if not customer:
        return jsonify({'success': False, 'error': 'Customer not found'}), 404

    notes = (
        Note.query
        .filter_by(customer_id=customer_id)
        .order_by(Note.call_date.asc())
        .all()
    )
    if not notes:
        return jsonify({'success': False, 'error': 'No notes found for this customer'}), 400

    # Build note payloads (shared by both paths)
    import re as _re
    note_payloads = []
    for cl in notes:
        date_str = cl.call_date.strftime('%Y-%m-%d')
        content = _re.sub(r'<[^>]+>', '', cl.content or '')
        topics = [t.name for t in cl.topics] if cl.topics else []
        note_payloads.append({"date": date_str, "content": content, "topics": topics})

    overview = ''
    if customer.account_context:
        overview = _re.sub(r'<[^>]+>', '', customer.account_context)

    try:
        # ---- Gateway path ----
        if is_gateway_mode():
            result = gateway_call("/v1/engagement-summary", {
                "customer_name": customer.name,
                "tpid": customer.tpid or "",
                "overview": overview,
                "notes": note_payloads,
            })
            summary_text = result.get("summary", "")
            usage = result.get("usage", {})

            log_entry = AIQueryLog(
                request_text=f"Engagement summary for {customer.name} ({len(notes)} logs)",
                response_text=summary_text[:500],
                success=True,
                model=usage.get("model", "gateway"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
            db.session.add(log_entry)
            db.session.commit()

            return jsonify({
                'success': True,
                'summary': summary_text,
                'note_count': len(notes)
            })

        # ---- Direct / legacy path ----
        deployment_name = get_openai_deployment()

        call_text_parts = []
        for n in note_payloads:
            entry = f"[{n['date']}]"
            if n['topics']:
                entry += f" Topics: {', '.join(n['topics'])}"
            entry += f"\n{n['content']}"
            call_text_parts.append(entry)
        call_text = '\n\n---\n\n'.join(call_text_parts)

        MAX_CHARS = 30000
        if len(call_text) > MAX_CHARS:
            call_text = call_text[:MAX_CHARS] + '\n\n[... additional notes truncated ...]'

        notes_section = ''
        if overview:
            notes_section = f"\nExisting Account Context:\n{overview}\n"

        user_message = (
            f"Customer: {customer.name} (TPID: {customer.tpid})\n"
            f"Total notes: {len(notes)}\n"
            f"{notes_section}\n"
            f"Notes:\n\n{call_text}"
        )

        client = get_azure_openai_client()
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": ENGAGEMENT_SUMMARY_PROMPT},
                {"role": "user", "content": user_message}
            ],
            max_tokens=1000,
            model=deployment_name
        )

        response_text = response.choices[0].message.content
        if not response_text or not response_text.strip():
            raise ValueError("AI returned empty content")
        response_text = response_text.strip()

        log_entry = AIQueryLog(
            request_text=f"Engagement summary for {customer.name} ({len(notes)} logs)",
            response_text=response_text[:500],
            success=True,
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({
            'success': True,
            'summary': response_text,
            'note_count': len(notes)
        })

    except (GatewayError, Exception) as e:
        log_entry = AIQueryLog(
            request_text=f"Engagement summary for {customer.name} ({len(notes)} logs)",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({'success': False, 'error': f'AI request failed: {e}'}), 500


ENGAGEMENT_STORY_PROMPT = (
    "You are a Microsoft technical seller's assistant. Analyze the provided notes "
    "for a specific customer engagement and generate structured story fields.\n\n"
    "Return your response as valid JSON with EXACTLY these keys:\n"
    "{\n"
    '  "key_individuals": "names and titles of key people involved",\n'
    '  "technical_problem": "the technical or business challenges they face",\n'
    '  "business_impact": "how the problem impacts their business processes/strategy",\n'
    '  "solution_resources": "Azure services, tools, or approaches being used",\n'
    '  "estimated_acr": "expected monthly/annual Azure consumption revenue impact",\n'
    '  "target_date": "target completion date in YYYY-MM-DD format, or null if unknown"\n'
    "}\n\n"
    "Rules:\n"
    "- Be concise but specific. Use actual details from the notes.\n"
    "- If a field cannot be determined, use null for that field.\n"
    "- For target_date, only return a date string if a specific date or timeframe is mentioned.\n"
    "- For estimated_acr, include dollar amounts if mentioned (e.g. '$5,000/mo ACR').\n"
    "- Return ONLY the JSON object, no markdown formatting or extra text."
)


@ai_bp.route('/api/ai/generate-engagement-story', methods=['POST'])
def api_ai_generate_engagement_story():
    """Generate structured story fields for a specific engagement from its linked notes."""
    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400

    data = request.get_json()
    engagement_id = data.get('engagement_id') if data else None
    if not engagement_id:
        return jsonify({'success': False, 'error': 'engagement_id is required'}), 400

    from app.models import Engagement
    engagement = Engagement.query.get(engagement_id)
    if not engagement:
        return jsonify({'success': False, 'error': 'Engagement not found'}), 404

    customer = engagement.customer
    notes = sorted(engagement.notes, key=lambda n: n.call_date)
    if not notes:
        return jsonify({
            'success': False,
            'error': 'No notes linked to this engagement. Link some notes first.'
        }), 400

    # Build the user_message (shared by both paths)
    import re as _re
    call_text_parts = []
    for cl in notes:
        date_str = cl.call_date.strftime('%Y-%m-%d')
        content = _re.sub(r'<[^>]+>', '', cl.content or '')
        topics = ', '.join(t.name for t in cl.topics) if cl.topics else ''
        entry = f"[{date_str}]"
        if topics:
            entry += f" Topics: {topics}"
        entry += f"\n{content}"
        call_text_parts.append(entry)

    call_text = '\n\n---\n\n'.join(call_text_parts)
    MAX_CHARS = 30000
    if len(call_text) > MAX_CHARS:
        call_text = call_text[:MAX_CHARS] + '\n\n[... additional notes truncated ...]'

    engagement_context = f"Engagement: {engagement.title}\n"
    if engagement.key_individuals:
        engagement_context += f"Current Key Individuals: {engagement.key_individuals}\n"
    if engagement.technical_problem:
        engagement_context += f"Current Technical Problem: {engagement.technical_problem}\n"

    opp_context = ''
    if engagement.opportunities:
        opp_names = [o.name for o in engagement.opportunities]
        opp_context = f"Linked Opportunities: {', '.join(opp_names)}\n"
    if engagement.milestones:
        ms_names = [m.display_text for m in engagement.milestones]
        opp_context += f"Linked Milestones: {', '.join(ms_names)}\n"

    user_message = (
        f"Customer: {customer.name}\n"
        f"{engagement_context}"
        f"{opp_context}"
        f"Total notes: {len(notes)}\n\n"
        f"Notes:\n\n{call_text}"
    )

    try:
        # ---- Gateway path ----
        if is_gateway_mode():
            result = gateway_call("/v1/engagement-story", {
                "user_message": user_message,
            })
            story_data = result.get("story", {})
            usage = result.get("usage", {})

        # ---- Direct / legacy path ----
        else:
            deployment_name = get_openai_deployment()
            client = get_azure_openai_client()
            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": ENGAGEMENT_STORY_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                max_tokens=1000,
                model=deployment_name
            )
            response_text = response.choices[0].message.content
            if not response_text or not response_text.strip():
                raise ValueError("AI returned empty content")
            response_text = response_text.strip()

            if response_text.startswith('```'):
                response_text = _re.sub(r'^```(?:json)?\s*', '', response_text)
                response_text = _re.sub(r'\s*```$', '', response_text)

            story_data = json.loads(response_text)
            usage = {}

        # ---- Common: save story fields to engagement ----
        from datetime import datetime as _datetime
        if story_data.get('key_individuals'):
            engagement.key_individuals = story_data['key_individuals']
        if story_data.get('technical_problem'):
            engagement.technical_problem = story_data['technical_problem']
        if story_data.get('business_impact'):
            engagement.business_impact = story_data['business_impact']
        if story_data.get('solution_resources'):
            engagement.solution_resources = story_data['solution_resources']
        if story_data.get('estimated_acr'):
            engagement.estimated_acr = story_data['estimated_acr']
        if story_data.get('target_date'):
            try:
                engagement.target_date = _datetime.strptime(
                    story_data['target_date'], '%Y-%m-%d'
                ).date()
            except (ValueError, TypeError):
                pass

        log_entry = AIQueryLog(
            request_text=f"Story for engagement '{engagement.title}' ({len(notes)} notes)",
            response_text=json.dumps(story_data)[:500],
            success=True,
            model=usage.get("model", "gateway" if is_gateway_mode() else ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({
            'success': True,
            'story': story_data,
            'note_count': len(notes)
        })

    except (GatewayError, ValueError, KeyError, json.JSONDecodeError) as e:
        log_entry = AIQueryLog(
            request_text=f"Story for engagement '{engagement.title}' ({len(notes)} notes)",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({
            'success': False,
            'error': f'AI request failed: {e}'
        }), 500

    except Exception as e:
        log_entry = AIQueryLog(
            request_text=f"Story for engagement '{engagement.title}' ({len(notes)} notes)",
            response_text=None,
            success=False,
            error_message=str(e)[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        return jsonify({
            'success': False,
            'error': f'AI request failed: {e}'
        }), 500