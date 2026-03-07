"""
Connect Export routes for NoteHelper.

Provides functionality to export call log data for writing Microsoft Connects
(self-evaluations). Generates structured summaries and JSON exports scoped to
a configurable date range, with milestone revenue impact per customer.

V2 adds AI-assisted summary generation using Azure OpenAI to produce
polished Connect narratives from raw call log data.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import (
    Blueprint, Response, current_app, g, jsonify, redirect, render_template,
    request, url_for, flash,
)

from app.models import (
    CallLog, ConnectExport, Customer, Milestone, db,
)
from app.routes.ai import is_ai_enabled

connect_export_bp = Blueprint('connect_export', __name__)

# ---------------------------------------------------------------------------
# Constants for AI summary generation
# ---------------------------------------------------------------------------

# Rough chars-per-token ratio (conservative for English text)
CHARS_PER_TOKEN = 4

# Maximum input tokens to send in a single AI call.  GPT-4o-mini supports
# 128K context; we leave headroom for the system prompt (~600 tokens) and the
# completion (~2K tokens).
MAX_INPUT_TOKENS = 100_000

# When chunking by customer, each chunk targets this many tokens so we stay
# well under the per-call limit while leaving room for the summary header that
# rides along with every chunk.
CHUNK_TARGET_TOKENS = 80_000

# System prompt for single-call (full export fits in context)
CONNECT_SUMMARY_SYSTEM_PROMPT = (
    "You are an expert at writing Microsoft Connect self-evaluations for "
    "Azure technical sellers. You will receive structured call log data "
    "covering a specific date range. Your job is to produce content the "
    "seller can paste directly into their Connect form.\n\n"
    "The Connect form has 3 fields. Write each as a separate Markdown section:\n\n"
    "## What results did you deliver, and how did you do it?\n"
    "- Focus on IMPACT, not just activity. Highlight outcomes and results.\n"
    "- Use specific examples with metrics where possible "
    "(e.g., 'influenced $X in milestone revenue' or 'engaged X customers on Azure AI').\n"
    "- Demonstrate WHAT you delivered (results for goals in Analytics, Databases, AI, etc.) "
    "and HOW you worked (behaviors that helped customers/others succeed).\n\n"
    "## Reflect on setbacks - what did you learn?\n"
    "- Be honest and self-aware based on what the data shows.\n"
    "- Identify areas where engagement could have been deeper or where gaps exist.\n"
    "- Share what could be done differently and growth opportunities.\n\n"
    "## What are your goals for the upcoming period?\n"
    "- Keep it focused: 2-3 high-impact, achievable goals based on the trends in the data.\n"
    "- Align goals with the technology themes and customer needs from this period.\n"
    "- Clarify expected outcomes for each goal.\n\n"
    "Tips (follow these strictly):\n"
    "- Be concise. Use bullet points, not paragraphs.\n"
    "- Quantify your impact wherever you can.\n"
    "- Avoid routine tasks. Focus on outcomes that moved the needle.\n"
    "- Write in first person ('I engaged...', 'I helped...').\n"
    "- Do not invent information that isn't in the data.\n"
)

# System prompt for per-chunk calls (subset of customers)
CONNECT_CHUNK_SYSTEM_PROMPT = (
    "You are an expert at writing Microsoft Connect self-evaluations for "
    "Azure technical sellers. You will receive a subset of call log data for "
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

# System prompt for the synthesis call that combines chunk summaries
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
    "- Identify areas where engagement could have been deeper.\n"
    "- Share what could be done differently.\n\n"
    "## What are your goals for the upcoming period?\n"
    "- Keep it focused: 2-3 high-impact, achievable goals based on trends.\n"
    "- Align goals with technology themes and customer needs.\n"
    "- Clarify expected outcomes for each goal.\n\n"
    "Tips (follow these strictly):\n"
    "- Be concise. Use bullet points, not paragraphs.\n"
    "- Quantify your impact wherever you can.\n"
    "- Avoid routine tasks. Focus on outcomes that moved the needle.\n"
    "- Write in first person.\n"
    "- Do not invent information or repeat partial summaries verbatim.\n"
)

# HTML tag stripper for plain-text output
_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(html_text: str) -> str:
    """Remove HTML tags and collapse whitespace for plain-text output."""
    if not html_text:
        return ''
    text = _TAG_RE.sub('', html_text)
    # Collapse multiple newlines/spaces
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def _build_export_data(start_date: date, end_date: date) -> dict[str, Any]:
    """
    Query all call logs in the date range and build structured export data.

    Returns a dict with:
        - summary: aggregate counts and topic/customer breakdowns
        - customers: per-customer detail with call logs, topics, milestone revenue
    """
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload

    # Convert dates to datetime range for query (inclusive of end_date)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    # Query call logs in range with eager loading
    call_logs = (
        CallLog.query
        .filter(
            CallLog.call_date >= start_dt,
            CallLog.call_date <= end_dt,
        )
        .options(
            joinedload(CallLog.customer).joinedload(Customer.seller),
            joinedload(CallLog.customer).joinedload(Customer.territory),
            joinedload(CallLog.topics),
            joinedload(CallLog.milestones),
        )
        .order_by(CallLog.call_date.asc())
        .all()
    )

    # Group by customer
    customers_map: dict[int, dict] = {}
    topic_counts: dict[str, dict] = {}  # topic_name -> {count, customers set}
    general_notes: list[dict] = []  # Notes not associated with a customer

    for cl in call_logs:
        cust = cl.customer
        if not cust:
            # General note (not customer-associated)
            topics_list = [t.name for t in cl.topics]
            general_notes.append({
                'id': cl.id,
                'date': cl.call_date.strftime('%Y-%m-%d'),
                'content': cl.content,
                'content_text': _strip_html(cl.content),
                'topics': topics_list,
            })
            # Still track topics from general notes
            for topic_name in topics_list:
                if topic_name not in topic_counts:
                    topic_counts[topic_name] = {'count': 0, 'customers': set()}
                topic_counts[topic_name]['count'] += 1
                topic_counts[topic_name]['customers'].add('General Notes')
            continue

        cust_id = cust.id
        if cust_id not in customers_map:
            customers_map[cust_id] = {
                'id': cust.id,
                'name': cust.get_display_name(),
                'tpid': cust.tpid,
                'seller': cust.seller.name if cust.seller else None,
                'territory': cust.territory.name if cust.territory else None,
                'call_logs': [],
                'topics': set(),
                'milestone_revenue': 0.0,
                'milestone_count': 0,
            }

        # Add call log
        topics_list = [t.name for t in cl.topics]
        customers_map[cust_id]['call_logs'].append({
            'id': cl.id,
            'date': cl.call_date.strftime('%Y-%m-%d'),
            'content': cl.content,
            'content_text': _strip_html(cl.content),
            'topics': topics_list,
        })
        customers_map[cust_id]['topics'].update(topics_list)

        # Track topic counts
        for topic_name in topics_list:
            if topic_name not in topic_counts:
                topic_counts[topic_name] = {'count': 0, 'customers': set()}
            topic_counts[topic_name]['count'] += 1
            topic_counts[topic_name]['customers'].add(cust.get_display_name())

    # Get milestone revenue per customer (completed milestones where user is on team)
    for cust_id, cust_data in customers_map.items():
        completed_milestones = (
            Milestone.query
            .filter(
                Milestone.customer_id == cust_id,
                Milestone.on_my_team == True,
                Milestone.msx_status == 'Completed',
            )
            .all()
        )
        # Filter to milestones that were updated in the period
        for ms in completed_milestones:
            if ms.updated_at and start_dt <= ms.updated_at <= end_dt:
                cust_data['milestone_revenue'] += ms.dollar_value or 0
                cust_data['milestone_count'] += 1

    # Convert sets to sorted lists for serialization
    for cust_data in customers_map.values():
        cust_data['topics'] = sorted(cust_data['topics'])

    # Sort customers by call log count descending
    customers_list = sorted(
        customers_map.values(),
        key=lambda c: len(c['call_logs']),
        reverse=True,
    )

    # Build topic summary (sorted by count descending)
    topic_summary = [
        {
            'name': name,
            'call_count': data['count'],
            'customer_count': len(data['customers']),
            'customers': sorted(data['customers']),
        }
        for name, data in sorted(
            topic_counts.items(), key=lambda x: x[1]['count'], reverse=True
        )
    ]

    # Total milestone revenue
    total_milestone_revenue = sum(c['milestone_revenue'] for c in customers_list)
    total_milestone_count = sum(c['milestone_count'] for c in customers_list)

    # Unique customers and topics
    unique_customer_count = len(customers_list)
    unique_topic_count = len(topic_summary)

    summary = {
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'total_call_logs': len(call_logs),
        'unique_customers': unique_customer_count,
        'unique_topics': unique_topic_count,
        'general_notes_count': len(general_notes),
        'total_milestone_revenue': total_milestone_revenue,
        'total_milestone_count': total_milestone_count,
        'topics': topic_summary,
    }

    return {
        'summary': summary,
        'customers': customers_list,
        'general_notes': general_notes,
    }


def _format_currency(amount: float) -> str:
    """Format a dollar amount with commas and no cents."""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:,.1f}M"
    elif amount >= 1_000:
        return f"${amount / 1_000:,.1f}K"
    else:
        return f"${amount:,.0f}"


def _build_text_export(data: dict, name: str) -> str:
    """Build a copy-pastable plain-text export from structured data."""
    summary = data['summary']
    customers = data['customers']

    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"{name}")
    lines.append(f"Period: {summary['start_date']} to {summary['end_date']}")
    lines.append(f"{'=' * 60}")
    lines.append("")
    lines.append(f"{summary['total_call_logs']} call logs across "
                 f"{summary['unique_customers']} customers")

    if summary['total_milestone_revenue'] > 0:
        lines.append(
            f"Influenced {_format_currency(summary['total_milestone_revenue'])} "
            f"of committed milestone revenue "
            f"({summary['total_milestone_count']} milestones)"
        )

    # Topic summary
    if summary['topics']:
        lines.append("")
        lines.append(f"--- Topics ({summary['unique_topics']}) ---")
        for topic in summary['topics']:
            customer_names = ', '.join(topic['customers'][:5])
            suffix = f", +{len(topic['customers']) - 5} more" if len(topic['customers']) > 5 else ""
            lines.append(
                f"  {topic['name']} ({topic['call_count']} calls, "
                f"{topic['customer_count']} customers): {customer_names}{suffix}"
            )

    # Per-customer detail
    lines.append("")
    lines.append(f"{'=' * 60}")
    lines.append("CUSTOMER DETAIL")
    lines.append(f"{'=' * 60}")

    for cust in customers:
        lines.append("")
        lines.append(f"--- {cust['name']} ({len(cust['call_logs'])} call logs) ---")
        if cust['seller']:
            lines.append(f"Seller: {cust['seller']}")
        if cust['territory']:
            lines.append(f"Territory: {cust['territory']}")
        if cust['topics']:
            lines.append(f"Topics: {', '.join(cust['topics'])}")
        if cust['milestone_revenue'] > 0:
            lines.append(
                f"Influenced {_format_currency(cust['milestone_revenue'])} "
                f"of committed milestone revenue "
                f"({cust['milestone_count']} milestones)"
            )
        lines.append("")

        for cl in cust['call_logs']:
            topic_str = f" [{', '.join(cl['topics'])}]" if cl['topics'] else ""
            lines.append(f"  [{cl['date']}]{topic_str}")
            # Indent call log content
            for content_line in cl['content_text'].splitlines():
                lines.append(f"    {content_line}")
            lines.append("")

    # General notes (not associated with a customer)
    general_notes = data.get('general_notes', [])
    if general_notes:
        lines.append("")
        lines.append(f"{'=' * 60}")
        lines.append(f"GENERAL NOTES ({len(general_notes)})")
        lines.append(f"{'=' * 60}")
        lines.append("")
        for cl in general_notes:
            topic_str = f" [{', '.join(cl['topics'])}]" if cl['topics'] else ""
            lines.append(f"  [{cl['date']}]{topic_str}")
            for content_line in cl['content_text'].splitlines():
                lines.append(f"    {content_line}")
            lines.append("")

    return '\n'.join(lines)


def _build_json_export(data: dict, name: str) -> dict:
    """Build the JSON export structure (summary + full customer detail)."""
    result = {
        'export_name': name,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'summary': data['summary'],
        'customers': data['customers'],
    }
    general_notes = data.get('general_notes', [])
    if general_notes:
        result['general_notes'] = general_notes
    return result


def _build_markdown_export(data: dict, name: str) -> str:
    """Build a Markdown-formatted export from structured data."""
    summary = data['summary']
    customers = data['customers']

    lines = []
    lines.append(f"# {name}")
    lines.append(f"**Period:** {summary['start_date']} to {summary['end_date']}")
    lines.append("")
    lines.append(f"{summary['total_call_logs']} call logs across "
                 f"{summary['unique_customers']} customers")

    if summary['total_milestone_revenue'] > 0:
        lines.append(
            f"Influenced **{_format_currency(summary['total_milestone_revenue'])}** "
            f"of committed milestone revenue "
            f"({summary['total_milestone_count']} milestones)"
        )

    # Topic summary
    if summary['topics']:
        lines.append("")
        lines.append(f"## Topics ({summary['unique_topics']})")
        lines.append("")
        for topic in summary['topics']:
            customer_names = ', '.join(topic['customers'][:5])
            suffix = f", +{len(topic['customers']) - 5} more" if len(topic['customers']) > 5 else ""
            lines.append(
                f"- **{topic['name']}** ({topic['call_count']} calls, "
                f"{topic['customer_count']} customers): {customer_names}{suffix}"
            )

    # Per-customer detail
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Customer Detail")

    for cust in customers:
        lines.append("")
        lines.append(f"### {cust['name']} ({len(cust['call_logs'])} call logs)")
        lines.append("")
        meta_parts = []
        if cust['seller']:
            meta_parts.append(f"**Seller:** {cust['seller']}")
        if cust['territory']:
            meta_parts.append(f"**Territory:** {cust['territory']}")
        if cust['topics']:
            meta_parts.append(f"**Topics:** {', '.join(cust['topics'])}")
        if meta_parts:
            lines.append(' | '.join(meta_parts))
            lines.append("")
        if cust['milestone_revenue'] > 0:
            lines.append(
                f"Influenced **{_format_currency(cust['milestone_revenue'])}** "
                f"of committed milestone revenue "
                f"({cust['milestone_count']} milestones)"
            )
            lines.append("")

        for cl in cust['call_logs']:
            topic_str = f" *[{', '.join(cl['topics'])}]*" if cl['topics'] else ""
            lines.append(f"**{cl['date']}**{topic_str}")
            lines.append("")
            lines.append(cl['content_text'])
            lines.append("")

    # General notes (not associated with a customer)
    general_notes = data.get('general_notes', [])
    if general_notes:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"## General Notes ({len(general_notes)})")
        lines.append("")
        for cl in general_notes:
            topic_str = f" *[{', '.join(cl['topics'])}]*" if cl['topics'] else ""
            lines.append(f"**{cl['date']}**{topic_str}")
            lines.append("")
            lines.append(cl['content_text'])
            lines.append("")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# AI summary helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return len(text) // CHARS_PER_TOKEN


def _build_summary_header(data: dict) -> str:
    """Build a compact stats header to include in every AI prompt chunk."""
    summary = data['summary']
    lines = [
        f"Period: {summary['start_date']} to {summary['end_date']}",
        f"Total call logs: {summary['total_call_logs']}",
        f"Unique customers: {summary['unique_customers']}",
        f"Unique topics: {summary['unique_topics']}",
    ]
    if summary['total_milestone_revenue'] > 0:
        lines.append(
            f"Total milestone revenue influenced: "
            f"{_format_currency(summary['total_milestone_revenue'])} "
            f"({summary['total_milestone_count']} milestones)"
        )
    if summary['topics']:
        topic_names = ', '.join(t['name'] for t in summary['topics'][:20])
        lines.append(f"Top topics: {topic_names}")
    return '\n'.join(lines)


def _build_customer_text_block(cust: dict) -> str:
    """Build the text block for a single customer (for chunking)."""
    lines = []
    lines.append(f"--- {cust['name']} ({len(cust['call_logs'])} call logs) ---")
    if cust.get('seller'):
        lines.append(f"Seller: {cust['seller']}")
    if cust.get('territory'):
        lines.append(f"Territory: {cust['territory']}")
    if cust.get('topics'):
        lines.append(f"Topics: {', '.join(cust['topics'])}")
    if cust.get('milestone_revenue', 0) > 0:
        lines.append(
            f"Influenced {_format_currency(cust['milestone_revenue'])} "
            f"of committed milestone revenue "
            f"({cust['milestone_count']} milestones)"
        )
    lines.append("")
    for cl in cust['call_logs']:
        topic_str = f" [{', '.join(cl['topics'])}]" if cl['topics'] else ""
        lines.append(f"  [{cl['date']}]{topic_str}")
        for content_line in cl.get('content_text', '').splitlines():
            lines.append(f"    {content_line}")
        lines.append("")
    return '\n'.join(lines)


def _build_general_notes_text_block(general_notes: list[dict]) -> str:
    """Build the text block for general notes (for chunking)."""
    if not general_notes:
        return ''
    lines = []
    lines.append(f"--- General Notes ({len(general_notes)} notes, not customer-associated) ---")
    lines.append("")
    for cl in general_notes:
        topic_str = f" [{', '.join(cl['topics'])}]" if cl['topics'] else ""
        lines.append(f"  [{cl['date']}]{topic_str}")
        for content_line in cl.get('content_text', '').splitlines():
            lines.append(f"    {content_line}")
        lines.append("")
    return '\n'.join(lines)


def _chunk_customers(data: dict, max_tokens: int = CHUNK_TARGET_TOKENS) -> list[list[dict]]:
    """
    Split customer list into chunks that each fit within *max_tokens*.

    The summary header rides along with every chunk, so its cost is
    subtracted from the budget up front.

    Returns a list of customer-lists (each list is one chunk).
    """
    header = _build_summary_header(data)
    header_tokens = _estimate_tokens(header)
    budget = max_tokens - header_tokens - 500  # 500 token buffer

    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    current_tokens = 0

    for cust in data['customers']:
        cust_text = _build_customer_text_block(cust)
        cust_tokens = _estimate_tokens(cust_text)

        if current_chunk and current_tokens + cust_tokens > budget:
            chunks.append(current_chunk)
            current_chunk = [cust]
            current_tokens = cust_tokens
        else:
            current_chunk.append(cust)
            current_tokens += cust_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _call_openai(system_prompt: str, user_prompt: str,
                 max_tokens: int = 2000) -> tuple[str, dict]:
    """
    Make a single Azure OpenAI chat completion call.

    Returns (response_text, usage_dict).
    """
    from app.routes.ai import get_azure_openai_client, get_openai_deployment

    client = get_azure_openai_client()
    deployment = get_openai_deployment()

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        model=deployment,
    )

    text = response.choices[0].message.content or ''
    usage = {
        'model': response.model or deployment,
        'prompt_tokens': response.usage.prompt_tokens if response.usage else 0,
        'completion_tokens': response.usage.completion_tokens if response.usage else 0,
        'total_tokens': response.usage.total_tokens if response.usage else 0,
    }
    return text.strip(), usage


def _generate_ai_summary_single(data: dict, text_export: str) -> tuple[str, dict]:
    """Generate an AI summary with a single API call (export fits in context)."""
    user_prompt = (
        "Here is my call log data for this Connect period.  "
        "Please write my Connect self-evaluation.\n\n"
        f"{text_export}"
    )
    return _call_openai(CONNECT_SUMMARY_SYSTEM_PROMPT, user_prompt, max_tokens=2000)


def _generate_ai_summary_chunked(data: dict, text_export: str) -> tuple[str, dict]:
    """
    Generate an AI summary by splitting customers into chunks, processing
    them in parallel, and running a final synthesis call.

    Returns (final_summary_text, aggregated_usage).
    """
    header = _build_summary_header(data)
    chunks = _chunk_customers(data)
    chunk_count = len(chunks)

    aggregated_usage = {
        'model': '',
        'prompt_tokens': 0,
        'completion_tokens': 0,
        'total_tokens': 0,
    }
    partial_summaries: list[str] = [''] * chunk_count

    def _process_chunk(index: int, customers: list[dict]) -> tuple[int, str, dict]:
        """Process a single chunk -- designed to run in a thread."""
        customer_text = '\n'.join(_build_customer_text_block(c) for c in customers)
        # Include general notes in the last chunk
        general_notes_text = ''
        if index == chunk_count - 1:
            gn = data.get('general_notes', [])
            if gn:
                general_notes_text = '\n\n' + _build_general_notes_text_block(gn)
        user_prompt = (
            f"Overall period stats:\n{header}\n\n"
            f"Customer details (chunk {index + 1} of {chunk_count}):\n\n"
            f"{customer_text}{general_notes_text}"
        )
        text, usage = _call_openai(CONNECT_CHUNK_SYSTEM_PROMPT, user_prompt, max_tokens=1500)
        return index, text, usage

    # Run chunk calls in parallel
    with ThreadPoolExecutor(max_workers=min(chunk_count, 4)) as executor:
        futures = [
            executor.submit(_process_chunk, i, customers)
            for i, customers in enumerate(chunks)
        ]
        for future in as_completed(futures):
            idx, text, usage = future.result()
            partial_summaries[idx] = text
            aggregated_usage['model'] = usage['model']
            aggregated_usage['prompt_tokens'] += usage['prompt_tokens']
            aggregated_usage['completion_tokens'] += usage['completion_tokens']
            aggregated_usage['total_tokens'] += usage['total_tokens']

    # Synthesis call: combine partial summaries into final narrative
    combined = '\n\n---\n\n'.join(
        f"### Chunk {i + 1}\n{s}" for i, s in enumerate(partial_summaries)
    )
    synthesis_prompt = (
        f"Overall period stats:\n{header}\n\n"
        f"Here are partial summaries from {chunk_count} customer groups:\n\n"
        f"{combined}"
    )
    final_text, synthesis_usage = _call_openai(
        CONNECT_SYNTHESIS_SYSTEM_PROMPT, synthesis_prompt, max_tokens=2000
    )
    aggregated_usage['prompt_tokens'] += synthesis_usage['prompt_tokens']
    aggregated_usage['completion_tokens'] += synthesis_usage['completion_tokens']
    aggregated_usage['total_tokens'] += synthesis_usage['total_tokens']

    return final_text, aggregated_usage


def generate_ai_summary(data: dict, text_export: str) -> tuple[str, dict]:
    """
    Generate an AI-powered Connect summary from export data.

    Automatically chooses single-call or chunked strategy based on
    estimated token count.

    Returns (summary_text, usage_dict).
    """
    input_tokens = _estimate_tokens(text_export)

    if input_tokens <= MAX_INPUT_TOKENS:
        return _generate_ai_summary_single(data, text_export)
    else:
        return _generate_ai_summary_chunked(data, text_export)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@connect_export_bp.route('/connect-export')
def connect_export_page():
    """Render the Connect Export page with date picker and previous exports."""
    user = g.user

    # Get previous exports for this user (most recent first)
    previous_exports = (
        ConnectExport.query
        .order_by(ConnectExport.created_at.desc())
        .all()
    )

    # Auto-populate dates: start = day after last export's end_date, end = today
    default_start = None
    default_end = date.today()

    if previous_exports:
        last_export = previous_exports[0]
        default_start = last_export.end_date + timedelta(days=1)

    return render_template(
        'connect_export.html',
        previous_exports=previous_exports,
        default_start=default_start.isoformat() if default_start else '',
        default_end=default_end.isoformat(),
        ai_enabled=is_ai_enabled(),
    )


@connect_export_bp.route('/api/connect-export/generate', methods=['POST'])
def generate_connect_export():
    """
    Generate a Connect export for the given date range.

    Expected JSON body:
        name: string (export name, e.g. "FY26 Final Connect")
        start_date: string (YYYY-MM-DD)
        end_date: string (YYYY-MM-DD)

    Returns JSON with summary and text/json export data.
    """
    if not request.is_json:
        return jsonify({'success': False, 'error': 'JSON body required'}), 400

    user = g.user
    name = request.json.get('name', '').strip()
    start_str = request.json.get('start_date', '').strip()
    end_str = request.json.get('end_date', '').strip()

    if not name:
        return jsonify({'success': False, 'error': 'Export name is required'}), 400
    if not start_str or not end_str:
        return jsonify({'success': False, 'error': 'Start and end dates are required'}), 400

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date format (use YYYY-MM-DD)'}), 400

    if start_date > end_date:
        return jsonify({'success': False, 'error': 'Start date must be before end date'}), 400

    # Build the export data
    data = _build_export_data(start_date, end_date)

    # Generate all formats
    text_export = _build_text_export(data, name)
    json_export = _build_json_export(data, name)
    markdown_export = _build_markdown_export(data, name)

    # Save the export record
    export_record = ConnectExport(
        name=name,
        start_date=start_date,
        end_date=end_date,
        call_log_count=data['summary']['total_call_logs'],
        customer_count=data['summary']['unique_customers'],
    )
    db.session.add(export_record)
    db.session.commit()

    return jsonify({
        'success': True,
        'export_id': export_record.id,
        'summary': data['summary'],
        'text_export': text_export,
        'json_export': json_export,
        'markdown_export': markdown_export,
    })


@connect_export_bp.route('/api/connect-export/<int:export_id>/view')
def view_connect_export(export_id: int):
    """View a previously generated Connect export (regenerates data from saved date range)."""
    user = g.user
    export_record = ConnectExport.query.filter_by(
        id=export_id,
    ).first()

    if not export_record:
        return jsonify({'success': False, 'error': 'Export not found'}), 404

    # Regenerate the data from the stored date range
    data = _build_export_data(export_record.start_date, export_record.end_date)
    text_export = _build_text_export(data, export_record.name)
    json_export = _build_json_export(data, export_record.name)
    markdown_export = _build_markdown_export(data, export_record.name)

    return jsonify({
        'success': True,
        'export_id': export_record.id,
        'name': export_record.name,
        'summary': data['summary'],
        'text_export': text_export,
        'json_export': json_export,
        'markdown_export': markdown_export,
        'ai_summary': export_record.ai_summary,
    })


@connect_export_bp.route('/api/connect-export/<int:export_id>/ai-summary', methods=['POST'])
def generate_connect_ai_summary(export_id: int):
    """
    Generate an AI-powered Connect summary for an existing export.

    Regenerates the structured data from the saved date range, feeds it to
    Azure OpenAI, and caches the result on the ConnectExport record.

    Automatically chunks large exports and processes them in parallel.
    """
    from app.models import AIQueryLog

    if not is_ai_enabled():
        return jsonify({
            'success': False,
            'error': 'AI features are not configured '
                     '(set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT in .env)',
        }), 400

    user = g.user
    export_record = ConnectExport.query.filter_by(
        id=export_id,
    ).first()

    if not export_record:
        return jsonify({'success': False, 'error': 'Export not found'}), 404

    # Regenerate the structured data from the stored date range
    data = _build_export_data(export_record.start_date, export_record.end_date)
    text_export = _build_text_export(data, export_record.name)

    if not data['customers']:
        return jsonify({
            'success': False,
            'error': 'No call log data found for this date range',
        }), 400

    # Estimate tokens for informational purposes
    estimated_tokens = _estimate_tokens(text_export)
    chunks_needed = len(_chunk_customers(data)) if estimated_tokens > MAX_INPUT_TOKENS else 1

    try:
        summary_text, usage = generate_ai_summary(data, text_export)

        # Cache the AI summary on the export record
        export_record.ai_summary = summary_text
        db.session.commit()

        # Log the AI query
        log_entry = AIQueryLog(
            request_text=f"Connect AI summary for '{export_record.name}' "
                         f"({export_record.start_date} to {export_record.end_date}), "
                         f"{estimated_tokens} est. input tokens, {chunks_needed} chunk(s)",
            response_text=summary_text[:500],
            success=True,
            model=usage.get('model', ''),
            prompt_tokens=usage.get('prompt_tokens'),
            completion_tokens=usage.get('completion_tokens'),
            total_tokens=usage.get('total_tokens'),
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({
            'success': True,
            'ai_summary': summary_text,
            'usage': usage,
            'chunks_used': chunks_needed,
        })

    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(f"Connect AI summary failed: {error_msg}")

        # Log the failure
        log_entry = AIQueryLog(
            request_text=f"Connect AI summary for '{export_record.name}' "
                         f"({export_record.start_date} to {export_record.end_date})",
            response_text=None,
            success=False,
            error_message=error_msg[:500],
        )
        db.session.add(log_entry)
        db.session.commit()

        return jsonify({
            'success': False,
            'error': f'AI request failed: {error_msg}',
        }), 500


@connect_export_bp.route('/api/connect-export/<int:export_id>', methods=['DELETE'])
def delete_connect_export(export_id: int):
    """Delete a Connect export record."""
    user = g.user
    export_record = ConnectExport.query.filter_by(
        id=export_id,
    ).first()

    if not export_record:
        return jsonify({'success': False, 'error': 'Export not found'}), 404

    db.session.delete(export_record)
    db.session.commit()

    return jsonify({'success': True})
