"""
AI routes for Sales Buddy.
Handles AI-powered topic suggestion and related features.

All AI calls go through the centralized APIM gateway
(Sales Buddy -> APIM -> App Service gateway -> Azure OpenAI).
No direct Azure OpenAI credentials are needed locally.

AI is always enabled -- the onboarding wizard enforces gateway
consent before users can access the product.
"""
from flask import Blueprint, request, jsonify, g
import json
import logging

from app.models import db, AIQueryLog, Topic
from app.gateway_client import gateway_call, GatewayError

logger = logging.getLogger(__name__)

# Create blueprint
ai_bp = Blueprint('ai', __name__)


@ai_bp.route('/api/ai/suggest-topics', methods=['POST'])
def api_ai_suggest_topics():
    """Generate topic suggestions from call notes using AI."""

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()

    if not call_notes or len(call_notes) < 10:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400

    try:
        existing_topics = [t.name for t in Topic.query.order_by(Topic.name).all()]
        result = gateway_call("/v1/suggest-topics", {
            "call_notes": call_notes,
            "existing_topics": existing_topics,
        })
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

        # Match suggested topics against existing DB topics (don't create new ones)
        topic_ids = []
        for topic_name in suggested_topics:
            existing_topic = Topic.query.filter(
                db.func.lower(Topic.name) == topic_name.lower()
            ).first()
            if existing_topic:
                topic_ids.append({'id': existing_topic.id, 'name': existing_topic.name})
            else:
                # Return without ID - frontend will hold as pending until save
                topic_ids.append({'id': None, 'name': topic_name})

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

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()
    milestones = data.get('milestones', [])

    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400
    if not milestones or len(milestones) == 0:
        return jsonify({'success': False, 'error': 'No milestones provided'}), 400

    try:
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

    except GatewayError as e:
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

    data = request.get_json()
    call_notes = data.get('call_notes', '').strip()

    if not call_notes or len(call_notes) < 20:
        return jsonify({'success': False, 'error': 'Call notes are too short to analyze'}), 400

    try:
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

        # Create / match topics in DB
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

    except GatewayError as e:
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


# NOTE: The engagement summary system prompt lives in infra/gateway/prompts.py
# and is used server-side by the gateway. No local copy needed.


@ai_bp.route('/api/ai/generate-engagement-summary', methods=['POST'])
def api_ai_generate_engagement_summary():
    """Generate a structured engagement summary from all call logs for a customer."""

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


# NOTE: The engagement story system prompt lives in infra/gateway/prompts.py
# and is used server-side by the gateway. No local copy needed.


@ai_bp.route('/api/ai/generate-engagement-story', methods=['POST'])
def api_ai_generate_engagement_story():
    """Generate structured story fields for a specific engagement from its linked notes."""

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
        ms_parts = []
        for m in engagement.milestones:
            part = m.display_text
            dollar_parts = []
            if m.monthly_usage:
                dollar_parts.append(f"${m.monthly_usage:,.0f}/mo usage")
            if m.dollar_value:
                dollar_parts.append(f"${m.dollar_value:,.0f} value")
            if dollar_parts:
                part += f" ({', '.join(dollar_parts)})"
            ms_parts.append(part)
        opp_context += f"Linked Milestones: {'; '.join(ms_parts)}\n"

    from datetime import datetime as _dt
    date_str = _dt.now().strftime('%Y-%m-%d')
    user_message = (
        f"Customer: {customer.name}\n"
        f"Today's date: {date_str}\n"
        f"{engagement_context}"
        f"{opp_context}"
        f"Total notes: {len(notes)}\n\n"
        f"Notes:\n\n{call_text}"
    )

    preview = data.get('preview', False)

    # Build current values for the review diff
    current = {
        'key_individuals': engagement.key_individuals or '',
        'technical_problem': engagement.technical_problem or '',
        'business_impact': engagement.business_impact or '',
        'solution_resources': engagement.solution_resources or '',
        'estimated_acr': engagement.estimated_acr or '',
        'target_date': (
            engagement.target_date.strftime('%Y-%m-%d')
            if engagement.target_date else ''
        ),
    }

    try:
        result = gateway_call("/v1/engagement-story", {
            "user_message": user_message,
        })
        story_data = result.get("story", {})
        usage = result.get("usage", {})

        log_entry = AIQueryLog(
            request_text=f"Story for engagement '{engagement.title}' ({len(notes)} notes)",
            response_text=json.dumps(story_data)[:500],
            success=True,
            model=usage.get("model", "gateway"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        db.session.add(log_entry)
        db.session.commit()

        if preview:
            return jsonify({
                'success': True,
                'story': story_data,
                'current': current,
                'note_count': len(notes)
            })

        # Non-preview: save all fields immediately (legacy behavior)
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
        db.session.commit()

        # Track engagement story on linked milestones
        if engagement.milestones:
            from app.services.milestone_tracking import track_engagement_on_milestones
            track_engagement_on_milestones(engagement)

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


@ai_bp.route('/api/ai/apply-engagement-story', methods=['POST'])
def api_ai_apply_engagement_story():
    """Apply user-selected story fields to an engagement."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    engagement_id = data.get('engagement_id')
    fields = data.get('fields', {})
    if not engagement_id:
        return jsonify({'success': False, 'error': 'engagement_id is required'}), 400
    if not fields:
        return jsonify({'success': False, 'error': 'No fields selected'}), 400

    from app.models import Engagement
    engagement = Engagement.query.get(engagement_id)
    if not engagement:
        return jsonify({'success': False, 'error': 'Engagement not found'}), 404

    allowed_fields = {
        'key_individuals', 'technical_problem', 'business_impact',
        'solution_resources', 'estimated_acr', 'target_date',
    }

    from datetime import datetime as _datetime
    for field_name, value in fields.items():
        if field_name not in allowed_fields:
            continue
        if field_name == 'target_date' and value:
            try:
                engagement.target_date = _datetime.strptime(
                    value, '%Y-%m-%d'
                ).date()
            except (ValueError, TypeError):
                pass
        else:
            setattr(engagement, field_name, value or None)

    db.session.commit()

    if engagement.milestones:
        from app.services.milestone_tracking import track_engagement_on_milestones
        track_engagement_on_milestones(engagement)

    return jsonify({'success': True})