"""
AI routes for NoteHelper.
Handles AI-powered topic suggestion and related features.
Uses Azure OpenAI with Entra ID (Service Principal) authentication.
All connection config comes from environment variables.
"""
from flask import Blueprint, request, jsonify, g
from datetime import date
import json
import os

from app.models import db, AIQueryLog, Topic

# Create blueprint
ai_bp = Blueprint('ai', __name__)

# System prompt for topic suggestion (the only AI call that uses a custom prompt)
TOPIC_SUGGESTION_PROMPT = (
    "You are a helpful assistant that analyzes call notes and suggests relevant topic tags. "
    "Based on the call notes provided, return a JSON array of 3-7 short topic tags (1-3 words each) "
    "that best describe the key technologies, products, or themes discussed. "
    "Return ONLY a JSON array of strings, nothing else. "
    'Example: ["Azure OpenAI", "Vector Search", "RAG Pattern"]'
)


def is_ai_enabled() -> bool:
    """Check if AI features are enabled based on environment configuration.
    
    AI is considered enabled when both AZURE_OPENAI_ENDPOINT and
    AZURE_OPENAI_DEPLOYMENT are set in environment variables.
    """
    return bool(
        os.environ.get('AZURE_OPENAI_ENDPOINT')
        and os.environ.get('AZURE_OPENAI_DEPLOYMENT')
    )


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
    
    # Check if AI features are enabled
    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT in .env)'}), 400
    
    deployment_name = get_openai_deployment()
    
    # Get call notes from request
    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()
    
    if not call_notes or len(call_notes) < 10:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400
    
    # Make AI API call
    try:
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
        
        # Keep the raw response for logging
        raw_response_text = response_text
        
        # Extract token usage and model info
        model_used = response.model or deployment_name
        prompt_tokens = response.usage.prompt_tokens if response.usage else None
        completion_tokens = response.usage.completion_tokens if response.usage else None
        total_tokens = response.usage.total_tokens if response.usage else None
        
        # Parse JSON response - be flexible with different formats
        suggested_topics = []
        try:
            # Remove markdown code blocks if present
            clean_text = response_text
            if '```' in clean_text:
                # Extract content between code blocks
                import re
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', clean_text, re.DOTALL)
                if match:
                    clean_text = match.group(1).strip()
                else:
                    # Try to find just the JSON array
                    clean_text = clean_text.replace('```json', '').replace('```', '').strip()
            
            # Try to find a JSON array in the text
            import re
            array_match = re.search(r'\[.*\]', clean_text, re.DOTALL)
            if array_match:
                clean_text = array_match.group(0)
            
            suggested_topics = json.loads(clean_text)
            
            if not isinstance(suggested_topics, list):
                raise ValueError("Response is not a list")
            
            # Filter to strings only and clean up
            suggested_topics = [str(t).strip() for t in suggested_topics if t and str(t).strip()]
            
            if not suggested_topics:
                raise ValueError("No topics returned")
            
        except (json.JSONDecodeError, ValueError) as e:
            # Log malformed response with full details for debugging
            log_entry = AIQueryLog(
                request_text=call_notes[:1000],
                response_text=raw_response_text[:1000],
                success=False,
                error_message=f"Parse error: {str(e)}",
                model=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens
            )
            db.session.add(log_entry)
            db.session.commit()
            return jsonify({
                'success': False,
                'error': f'AI returned invalid response format. Check audit log for raw response.'
            }), 500
        
        # Log successful query
        log_entry = AIQueryLog(
            request_text=call_notes[:1000],
            response_text=raw_response_text[:1000],
            success=True,
            error_message=None,
            model=model_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens
        )
        db.session.add(log_entry)
        db.session.commit()
        
        # Process topics: check if they exist, if not create them, then return IDs
        topic_ids = []
        for topic_name in suggested_topics:
            # Check if topic exists (case-insensitive)
            existing_topic = Topic.query.filter(
                db.func.lower(Topic.name) == topic_name.lower()
            ).first()
            
            if existing_topic:
                topic_ids.append({'id': existing_topic.id, 'name': existing_topic.name})
            else:
                # Create new topic
                new_topic = Topic(name=topic_name)
                db.session.add(new_topic)
                db.session.flush()  # Get the ID
                topic_ids.append({'id': new_topic.id, 'name': new_topic.name})
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'topics': topic_ids
        })
    
    except Exception as e:
        # Log failed query
        error_msg = str(e)
        
        log_entry = AIQueryLog(
            request_text=call_notes[:1000],
            response_text=None,
            success=False,
            error_message=error_msg[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        
        return jsonify({'success': False, 'error': f'AI request failed: {error_msg}'}), 500


@ai_bp.route('/api/ai/match-milestone', methods=['POST'])
def api_ai_match_milestone():
    """Match call notes to the most relevant milestone using AI."""
    
    # Check if AI features are enabled
    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400
    
    deployment_name = get_openai_deployment()
    
    # Get data from request
    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()
    milestones = data.get('milestones', [])
    
    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400
    
    if not milestones or len(milestones) == 0:
        return jsonify({'success': False, 'error': 'No milestones provided'}), 400
    
    # Format milestones for the AI
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
    
    try:
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
        
        # Log the query
        log_entry = AIQueryLog(
            request_text=f"Match milestone: {call_notes[:500]}...",
            response_text=response_text[:500],
            success=True
        )
        db.session.add(log_entry)
        db.session.commit()
        
        # Parse JSON response
        import re
        # Remove markdown code blocks if present
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
        
    except json.JSONDecodeError as e:
        return jsonify({
            'success': False,
            'error': f'Could not parse AI response: {response_text[:100]}'
        }), 500
        
    except Exception as e:
        error_msg = str(e)
        
        log_entry = AIQueryLog(
            request_text=f"Match milestone: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=error_msg[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        
        return jsonify({'success': False, 'error': f'AI request failed: {error_msg}'}), 500


@ai_bp.route('/api/ai/analyze-call', methods=['POST'])
def api_ai_analyze_call():
    """
    Analyze call notes to extract and auto-tag topics.
    This is the AI call in the auto-fill flow for topic matching.
    Task title/description come from WorkIQ, not OpenAI.
    
    Takes: call_notes (str)
    Returns: topics (list of {id, name})
    """
    
    # Check if AI features are enabled
    if not is_ai_enabled():
        return jsonify({'success': False, 'error': 'AI features are not configured'}), 400
    
    deployment_name = get_openai_deployment()
    
    # Get data from request
    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()
    
    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400
    
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
    
    try:
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
        
        # Extract token usage
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
        
        result = json.loads(clean_text)
        
        # Log successful query
        log_entry = AIQueryLog(
            request_text=f"Analyze call: {call_notes[:500]}...",
            response_text=response_text[:500],
            success=True,
            model=model_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens
        )
        db.session.add(log_entry)
        
        # Process topics: check if they exist, create if not
        topics = result.get('topics', [])
        topic_ids = []
        for topic_name in topics:
            if not topic_name or not str(topic_name).strip():
                continue
            topic_name = str(topic_name).strip()
            
            # Check if topic exists (case-insensitive)
            existing_topic = Topic.query.filter(
                db.func.lower(Topic.name) == topic_name.lower()
            ).first()
            
            if existing_topic:
                topic_ids.append({'id': existing_topic.id, 'name': existing_topic.name})
            else:
                # Create new topic
                new_topic = Topic(name=topic_name)
                db.session.add(new_topic)
                db.session.flush()
                topic_ids.append({'id': new_topic.id, 'name': new_topic.name})
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'topics': topic_ids
        })
        
    except json.JSONDecodeError as e:
        log_entry = AIQueryLog(
            request_text=f"Analyze call: {call_notes[:500]}...",
            response_text=response_text[:500] if 'response_text' in dir() else None,
            success=False,
            error_message=f"JSON parse error: {str(e)}"
        )
        db.session.add(log_entry)
        db.session.commit()
        
        return jsonify({
            'success': False,
            'error': f'Could not parse AI response'
        }), 500
        
    except Exception as e:
        error_msg = str(e)
        
        log_entry = AIQueryLog(
            request_text=f"Analyze call: {call_notes[:500]}...",
            response_text=None,
            success=False,
            error_message=error_msg[:500]
        )
        db.session.add(log_entry)
        db.session.commit()
        
        return jsonify({'success': False, 'error': f'AI request failed: {error_msg}'}), 500


# System prompt for customer engagement summary generation
ENGAGEMENT_SUMMARY_PROMPT = (
    "You are a Microsoft technical seller's assistant. Analyze the provided call log notes "
    "and any existing customer notes for a customer and generate a structured engagement summary. "
    "Fill in each field based on what you can extract from the call logs and notes. If a field "
    "cannot be determined from the available information, write 'Not identified in call logs' "
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
    "Be concise but specific. Use actual details from the call logs and notes, not generic "
    "statements. If multiple topics or workstreams exist, cover the most significant ones."
)


@ai_bp.route('/api/ai/generate-engagement-summary', methods=['POST'])
def api_ai_generate_engagement_summary():
    """Generate a structured engagement summary from all call logs for a customer.

    Reads all call logs for the given customer_id and sends them to Azure OpenAI
    to fill out the engagement metadata table (Ben's table from issue #25).
    """
    if not is_ai_enabled():
        return jsonify({
            'success': False,
            'error': 'AI features are not configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT in .env)'
        }), 400

    deployment_name = get_openai_deployment()

    data = request.get_json()
    customer_id = data.get('customer_id') if data else None

    if not customer_id:
        return jsonify({'success': False, 'error': 'customer_id is required'}), 400

    from app.models import Customer, CallLog
    customer = Customer.query.get(customer_id)
    if not customer:
        return jsonify({'success': False, 'error': 'Customer not found'}), 404

    # Get all call logs sorted by date
    call_logs = (
        CallLog.query
        .filter_by(customer_id=customer_id)
        .order_by(CallLog.call_date.asc())
        .all()
    )

    if not call_logs:
        return jsonify({'success': False, 'error': 'No call logs found for this customer'}), 400

    # Build the call log text for the prompt
    import re as _re
    call_text_parts = []
    for cl in call_logs:
        date_str = cl.call_date.strftime('%Y-%m-%d')
        # Strip HTML tags for cleaner AI input
        content = _re.sub(r'<[^>]+>', '', cl.content or '')
        topics = ', '.join(t.name for t in cl.topics) if cl.topics else ''
        entry = f"[{date_str}]"
        if topics:
            entry += f" Topics: {topics}"
        entry += f"\n{content}"
        call_text_parts.append(entry)

    call_text = '\n\n---\n\n'.join(call_text_parts)

    # Cap the input to avoid token limits (roughly 30k chars)
    MAX_CHARS = 30000
    if len(call_text) > MAX_CHARS:
        call_text = call_text[:MAX_CHARS] + '\n\n[... additional call logs truncated ...]'

    # Include existing customer notes as additional context if present
    notes_section = ''
    if customer.notes:
        notes_text = _re.sub(r'<[^>]+>', '', customer.notes)
        notes_section = f"\nExisting Customer Notes:\n{notes_text}\n"

    user_message = (
        f"Customer: {customer.name} (TPID: {customer.tpid})\n"
        f"Total call logs: {len(call_logs)}\n"
        f"{notes_section}\n"
        f"Call Log Notes:\n\n{call_text}"
    )

    try:
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

        # Log success
        log_entry = AIQueryLog(
            request_text=f"Engagement summary for {customer.name} ({len(call_logs)} logs)",
            response_text=response_text[:500],
            success=True,
            error_message=None
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({
            'success': True,
            'summary': response_text,
            'call_log_count': len(call_logs)
        })

    except Exception as e:
        error_msg = str(e)

        log_entry = AIQueryLog(
            request_text=f"Engagement summary for {customer.name} ({len(call_logs)} logs)",
            response_text=None,
            success=False,
            error_message=error_msg[:500]
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({'success': False, 'error': f'AI request failed: {error_msg}'}), 500