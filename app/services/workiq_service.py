"""
WorkIQ Integration Service

Handles querying WorkIQ for meeting data and transcript summaries.
Uses the npx-based WorkIQ CLI tool.
"""
import subprocess
import platform
import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Default meeting summary prompt template.
# Users can customize this in Settings. Use {title} and {date} as placeholders.
DEFAULT_SUMMARY_PROMPT = (
    'Summarize the meeting called {title} {date} in approximately 250 words. '
    'Include key discussion points, technologies mentioned, and any action items. '
    'Start directly with the summary content. Do not include introductory disclaimers, '
    'caveats about missing transcripts, or follow-up offers.'
)

# Task suggestion suffix - always appended server-side after the user's prompt.
# Not user-editable so it can't be accidentally broken.
_TASK_PROMPT_SUFFIX = (
    ' Also suggest a short task title (under 10 words) and a brief 1-2 sentence '
    'task description that captures the main follow-up action from this meeting. '
    'Format the task suggestion on separate lines starting with TASK_TITLE: and TASK_DESCRIPTION:'
)

# Connect impact extraction suffix - appended when user has the preference enabled.
# Asks WorkIQ to identify customer impact signals useful for Connect self-evaluations.
_CONNECT_IMPACT_SUFFIX = (
    ' Additionally, identify any customer impact signals from this meeting that would '
    'be relevant for a Microsoft Connect self-evaluation. List concrete examples of '
    'customer outcomes, adoption milestones, technical wins, or business value delivered. '
    'Format each signal on its own line in a block starting with CONNECT_IMPACT: '
    'Use a dash (-) prefix for each signal. If there are no clear impact signals, '
    'omit the CONNECT_IMPACT block entirely.'
)


# Patterns that indicate conversational AI preamble/footer to strip
_PREAMBLE_PATTERNS = [
    # "Here's what I can summarize..." style openers
    re.compile(r"^Here'?s\s+(?:what|a|the|my)\b.+?(?:\n|$)", re.IGNORECASE | re.MULTILINE),
    # "Please note an important limitation" disclaimers
    re.compile(r'^Please\s+note\b.+?(?:\n|$)', re.IGNORECASE | re.MULTILINE),
    # "Based on the meeting records/invitation/metadata" caveats
    re.compile(r'^Based\s+(?:strictly\s+)?on\s+(?:the\s+)?meeting\b.+?(?:\n|$)', re.IGNORECASE | re.MULTILINE),
    # "I don't have access to a transcript" disclaimers
    re.compile(r"^I\s+(?:don'?t|do\s+not)\s+have\s+access\b.+?(?:\n|$)", re.IGNORECASE | re.MULTILINE),
    # "Note:" or "Important:" disclaimers at start
    re.compile(r'^(?:Note|Important|Disclaimer|Caveat)\s*:.+?(?:\n|$)', re.IGNORECASE | re.MULTILINE),
]

_FOOTER_PATTERNS = [
    # "Let me know if you'd like" (must be before generic "If you'd like" to avoid partial match)
    re.compile(r"Let\s+me\s+know\s+if\b.+", re.IGNORECASE | re.DOTALL),
    # "If you'd like, I can also:" and everything after
    re.compile(r"If\s+you'?d?\s+like\b.+", re.IGNORECASE | re.DOTALL),
    # "I can also:" offers and everything after
    re.compile(r"I\s+can\s+also\s*:.+", re.IGNORECASE | re.DOTALL),
    # "Would you like me to" offers and everything after
    re.compile(r"Would\s+you\s+like\s+me\s+to\b.+", re.IGNORECASE | re.DOTALL),
]


def _clean_ai_preamble(text: str) -> str:
    """Strip conversational AI preamble, disclaimers, and follow-up offers.

    WorkIQ responses often include:
    - Opening disclaimers about missing transcripts
    - "Here's what I can summarize..." introductions
    - "If you'd like, I can also..." footers

    These are appropriate for conversational AI but not for structured notes.
    The actual meeting summary content is preserved.
    """
    if not text:
        return text

    # Strategy 1: If the response has --- dividers with content between them,
    # extract just the content between the first and last divider.
    divider_sections = re.split(r'\n---+\n', text)
    if len(divider_sections) >= 3:
        # Content is in the middle section(s), skip first (preamble) and last (footer)
        # But only if the first section looks like preamble
        first = divider_sections[0].strip()
        last = divider_sections[-1].strip()
        first_is_preamble = any(p.search(first) for p in _PREAMBLE_PATTERNS)
        last_is_footer = any(p.search(last) for p in _FOOTER_PATTERNS) or not last

        if first_is_preamble or last_is_footer:
            middle = divider_sections[1:-1] if last_is_footer else divider_sections[1:]
            if first_is_preamble:
                pass  # already slicing from [1:]
            else:
                middle = divider_sections[:-1] if last_is_footer else divider_sections
            # Re-join just the content sections
            if first_is_preamble and last_is_footer:
                text = '\n---\n'.join(divider_sections[1:-1]).strip()
            elif first_is_preamble:
                text = '\n---\n'.join(divider_sections[1:]).strip()
            elif last_is_footer:
                text = '\n---\n'.join(divider_sections[:-1]).strip()

    # Strategy 2: Line-by-line preamble removal at the start of text.
    # Remove leading blank lines and preamble-matching lines.
    lines = text.split('\n')
    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            start_idx = i + 1
            continue
        if any(p.match(stripped) for p in _PREAMBLE_PATTERNS):
            start_idx = i + 1
            continue
        break  # Hit real content
    if start_idx > 0:
        text = '\n'.join(lines[start_idx:])

    # Strategy 3: Remove footer patterns ("If you'd like..." and everything after)
    for pattern in _FOOTER_PATTERNS:
        text = pattern.sub('', text)

    # Clean up: remove trailing --- dividers and whitespace
    text = re.sub(r'\n---+\s*$', '', text).strip()

    # Remove orphaned footnote reference numbers (e.g. " 1" at end of paragraphs)
    # These are WorkIQ citation markers that have no corresponding footnote
    text = re.sub(r'\s+\d+\s*$', '', text, flags=re.MULTILINE)

    return text


def _clean_meeting_title(title: str) -> str:
    """Clean WorkIQ meeting title by removing markdown links, attendee metadata, etc.
    
    WorkIQ sometimes returns titles like:
      Mercalis (external attendees with `@mercalis.com`) [1](https://teams.microsoft.com/...)
    
    This strips it down to just: Mercalis
    """
    if not title:
        return title
    
    # Remove markdown links: [text](url) or [number](url)
    title = re.sub(r'\[\d+\]\([^)]+\)', '', title)
    title = re.sub(r'\[[^\]]*\]\([^)]+\)', '', title)
    
    # Remove (external attendees with ...) and (external participants ...) metadata
    title = re.sub(r'\(external\s+(?:attendees|participants)\s+[^)]*\)', '', title, flags=re.IGNORECASE)
    
    # Remove any remaining URLs
    title = re.sub(r'https?://\S+', '', title)
    
    # Remove backtick-wrapped emails/domains
    title = re.sub(r'`[^`]*`', '', title)
    
    # Clean up whitespace and trailing punctuation
    title = re.sub(r'\s+', ' ', title).strip()
    title = title.strip('* ·-')
    
    return title


def fuzzy_match_score(text1: str, text2: str) -> float:
    """
    Calculate fuzzy match score between two strings.
    Returns a value between 0.0 and 1.0.
    """
    if not text1 or not text2:
        return 0.0
    
    # Normalize for comparison: lowercase and remove common words
    def normalize(s):
        s = s.lower().strip()
        # Remove common suffixes/words that don't help matching
        for word in ['inc', 'inc.', 'llc', 'corp', 'corporation', 'ltd', 'company', 'co']:
            s = s.replace(word, '')
        return s.strip()
    
    t1 = normalize(text1)
    t2 = normalize(text2)
    
    # Direct substring check (more generous)
    if t1 in t2 or t2 in t1:
        return 0.9
    
    # Use SequenceMatcher for fuzzy ratio
    return SequenceMatcher(None, t1, t2).ratio()


def query_workiq(question: str, timeout: int = 120) -> str:
    """
    Run a WorkIQ query and return the response.
    
    Args:
        question: The natural language question to ask WorkIQ
        timeout: Maximum seconds to wait for response
        
    Returns:
        The text response from WorkIQ
        
    Raises:
        TimeoutError: If query takes longer than timeout
        RuntimeError: If WorkIQ query fails
    """
    import shutil
    import os
    
    is_windows = platform.system() == 'Windows'
    
    # Find npx executable
    npx_path = shutil.which('npx')
    if not npx_path and is_windows:
        for path in [
            os.path.expandvars(r'%APPDATA%\npm\npx.cmd'),
            os.path.expandvars(r'%ProgramFiles%\nodejs\npx.cmd'),
        ]:
            if os.path.exists(path):
                npx_path = path
                break
    
    if not npx_path:
        raise RuntimeError("npx not found. Please install Node.js.")
    
    logger.info(f"Querying WorkIQ: {question[:100]}...")
    
    try:
        if is_windows:
            # On Windows, use PowerShell to avoid cmd.exe escaping issues
            # PowerShell handles special characters in strings properly
            # Escape single quotes by doubling them (PowerShell string escape)
            escaped_q = question.replace("'", "''")
            
            # Build PowerShell command
            ps_cmd = f"& '{npx_path}' -y @microsoft/workiq ask -q '{escaped_q}'"
            
            cmd = ["powershell", "-NoProfile", "-Command", ps_cmd]
            logger.info(f"Running PowerShell command for WorkIQ...")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False
            )
        else:
            # On Unix, list format works perfectly
            cmd = [npx_path, "-y", "@microsoft/workiq", "ask", "-q", question]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False
            )
        
        if result.returncode != 0:
            logger.error(f"WorkIQ error: {result.stderr}")
            raise RuntimeError(f"WorkIQ query failed: {result.stderr}")
        
        logger.info(f"WorkIQ response received ({len(result.stdout)} chars)")
        return result.stdout
        
    except subprocess.TimeoutExpired:
        logger.error(f"WorkIQ query timed out after {timeout}s")
        raise TimeoutError(f"WorkIQ query timed out after {timeout} seconds")


def get_meetings_for_date(date_str: str) -> List[Dict[str, Any]]:
    """
    Get all meetings for a specific date.
    
    Fetches ALL meetings (not just external) because WorkIQ's external-attendee
    detection is unreliable. The client-side customer matching step will
    filter out meetings that don't correspond to any known customer.
    
    Args:
        date_str: Date in YYYY-MM-DD format
        
    Returns:
        List of meeting dicts with keys:
        - id: Unique identifier (derived from title + time)
        - title: Meeting title
        - start_time: Meeting start time as datetime
        - customer: Extracted customer/company name
        - attendees: List of attendee names
    """
    question = (
        f"List all my meetings on {date_str} in a markdown table with columns: "
        f"Time, Meeting Title, and External Company (if any external attendees, "
        f"otherwise leave blank). Include ALL meetings, even internal ones."
    )
    
    try:
        response = query_workiq(question)
        logger.info(f"WorkIQ raw response length: {len(response)}, first 200 chars: {response[:200]}")
        meetings = _parse_meetings_response(response, date_str)
        logger.info(f"Parsed {len(meetings)} meetings from response")
        return meetings
    except Exception as e:
        logger.error(f"Failed to get meetings for {date_str}: {e}")
        return []


def _parse_meetings_response(response: str, date_str: str) -> List[Dict[str, Any]]:
    """Parse WorkIQ meeting list response into structured data.
    
    Handles WorkIQ's markdown table format like:
    | Time | Meeting Title | External Company |
    | 11:00 AM - 11:30 AM | **Fabric Meeting - CCFI** | CCFI |
    
    Also handles numbered list format:
    1. **Meeting Title**
       **9:00 AM** · Organizer: Name
    """
    meetings = []
    
    if not response:
        return meetings
    
    # Only bail on "no meetings" if the response is short and clearly empty
    # (don't bail if WorkIQ says "no external meetings" but still provides a table)
    lower_resp = response.lower()
    if 'no meetings' in lower_resp and '|' not in response and '1.' not in response:
        return meetings
    
    # Normalize non-standard whitespace characters (WorkIQ sometimes uses
    # U+00FF, U+00A0, and other non-breaking characters instead of spaces)
    response = re.sub(r'[\u00ff\u00a0\u2007\u202f\u2009\u200a]', ' ', response)
    
    # Try table format first (most common from WorkIQ)
    # Look for lines that start with | and contain times
    time_pattern = re.compile(r'(\d{1,2}:\d{2}\s*(?:AM|PM))', re.IGNORECASE)
    
    # Detect column layout from header row (WorkIQ varies column order)
    # Default: time=0, title=1, company=2
    title_col_idx = None
    time_col_idx = None
    company_col_idx = None
    
    lines = response.split('\n')
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith('|') and ('Meeting' in line_stripped or 'Title' in line_stripped):
            temp = line_stripped.replace('\\|', '\x00')
            header_parts = [p.replace('\x00', '|').strip().lower() for p in temp.split('|')]
            header_parts = [p for p in header_parts if p]
            for i, h in enumerate(header_parts):
                if 'title' in h or 'meeting' in h:
                    title_col_idx = i
                elif 'time' in h:
                    time_col_idx = i
                elif 'company' in h or 'external' in h or 'customer' in h:
                    company_col_idx = i
            break
    
    for line in lines:
        line = line.strip()
        
        # Skip header row or separator rows
        if not line.startswith('|') or '---' in line or ('Time' in line and 'Meeting' in line):
            continue
        
        # Split carefully on unescaped pipes
        # Replace escaped pipes temporarily
        temp = line.replace('\\|', '\x00')
        parts = [p.replace('\x00', '|').strip() for p in temp.split('|')]
        
        # Filter empty parts (from leading/trailing pipes)
        parts = [p for p in parts if p]
        
        # Need at least time and title
        if len(parts) < 2:
            continue
        
        # Find which part has the time
        time_col = None
        time_str = None
        for i, part in enumerate(parts):
            match = time_pattern.search(part)
            if match:
                time_col = i
                time_str = match.group(1)
                break
        
        if time_str is None:
            continue
        
        # Use header-detected column layout if available
        if title_col_idx is not None:
            title = parts[title_col_idx] if len(parts) > title_col_idx else ''
            company = parts[company_col_idx] if company_col_idx is not None and len(parts) > company_col_idx else ''
        else:
            # Fallback: assume title is after time, company after that
            title = parts[time_col + 1] if len(parts) > time_col + 1 else ''
            company = parts[time_col + 2] if len(parts) > time_col + 2 else ''
        
        # Clean up title - remove bold markers and WorkIQ metadata
        title = title.strip('* ')
        title = _clean_meeting_title(title)
        company = company.strip('* ')
        company = _clean_meeting_title(company)
        
        meeting = {
            'id': '',
            'title': title,
            'start_time': None,
            'start_time_str': time_str,
            'customer': company,
            'attendees': []
        }
        
        # Parse time
        try:
            time_obj = datetime.strptime(time_str, '%I:%M %p').time()
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            meeting['start_time'] = datetime.combine(date_obj, time_obj)
        except ValueError as e:
            logger.warning(f"Could not parse time '{time_str}': {e}")
        
        # Generate ID
        id_base = f"{date_str}_{title}_{time_str}"
        meeting['id'] = re.sub(r'[^a-zA-Z0-9_]', '', id_base.replace(' ', '_'))[:50]
        meetings.append(meeting)
    
    if meetings:
        logger.info(f"Parsed {len(meetings)} meetings from WorkIQ table format")
        return meetings
    
    # Fallback: try numbered/bulleted list format
    # Pattern to match: "1. **Meeting Title**" or "- **Meeting Title**"
    # Time may be in parentheses: "- **Title** (8:00-9:00 AM)"
    title_pattern = re.compile(r'(?:^|\n)\s*(?:\d+\.|-)\s*\*\*([^*]+)\*\*', re.MULTILINE)
    time_pattern_list = re.compile(r'\(?\s*(\d{1,2}:\d{2})\s*(?:-\s*\d{1,2}:\d{2})?\s*(AM|PM)\s*\)?', re.IGNORECASE)
    
    # Split on both numbered items and bullet points
    sections = re.split(r'\n(?=\s*(?:\d+\.|-)\s*\*\*)', response)
    
    for section in sections:
        if not section.strip():
            continue
        
        title_match = title_pattern.search(section)
        if not title_match:
            continue
            
        title = _clean_meeting_title(title_match.group(1).strip())
        
        meeting = {
            'id': '',
            'title': title,
            'start_time': None,
            'start_time_str': '',
            'customer': '',
            'attendees': []
        }
        
        time_match = time_pattern_list.search(section)
        if time_match:
            time_str = time_match.group(1)
            am_pm = time_match.group(2) or ''
            meeting['start_time_str'] = f"{time_str} {am_pm}".strip()
            
            try:
                if am_pm:
                    time_obj = datetime.strptime(f"{time_str} {am_pm}", '%I:%M %p').time()
                else:
                    time_obj = datetime.strptime(time_str, '%H:%M').time()
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                meeting['start_time'] = datetime.combine(date_obj, time_obj)
            except ValueError as e:
                logger.warning(f"Could not parse time '{time_str} {am_pm}': {e}")
        
        # Extract customer from title
        if '|' in title:
            meeting['customer'] = title.split('|')[0].strip()
        elif ' - ' in title:
            meeting['customer'] = title.split(' - ')[0].strip()
        
        id_base = f"{date_str}_{title}_{meeting['start_time_str']}"
        meeting['id'] = re.sub(r'[^a-zA-Z0-9_]', '', id_base.replace(' ', '_'))[:50]
        meetings.append(meeting)
    
    logger.info(f"Parsed {len(meetings)} meetings from WorkIQ list format")
    return meetings


def find_best_customer_match(meetings: List[Dict[str, Any]], customer_name: str) -> Optional[int]:
    """
    Find the best matching meeting for a customer name.
    
    Args:
        meetings: List of meeting dicts from get_meetings_for_date
        customer_name: Customer name to match against
        
    Returns:
        Index of best matching meeting, or None if no good match (score < 0.5)
    """
    if not meetings or not customer_name:
        return None
    
    best_score = 0.0
    best_index = None
    
    for i, meeting in enumerate(meetings):
        # Check against meeting title and customer fields
        title_score = fuzzy_match_score(customer_name, meeting.get('title', ''))
        customer_score = fuzzy_match_score(customer_name, meeting.get('customer', ''))
        
        # Take the better match
        score = max(title_score, customer_score)
        
        if score > best_score:
            best_score = score
            best_index = i
    
    # Only return if score is decent (> 0.5)
    if best_score >= 0.5:
        logger.info(f"Fuzzy matched '{customer_name}' to meeting {best_index} with score {best_score:.2f}")
        return best_index
    
    return None


def get_meeting_summary(meeting_title: str, date_str: str = None,
                        custom_prompt: str = None,
                        extract_impact: bool = False) -> Dict[str, Any]:
    """
    Get a detailed 250-word summary for a specific meeting.
    
    Args:
        meeting_title: The meeting title to summarize
        date_str: Optional date for context (YYYY-MM-DD)
        custom_prompt: Optional custom prompt template. Use {title} and {date}
                      as placeholders. Falls back to DEFAULT_SUMMARY_PROMPT.
        extract_impact: If True, append Connect impact extraction suffix to prompt
        
    Returns:
        Dict with:
        - summary: The 250-word summary text
        - topics: List of technologies/topics discussed
        - action_items: List of follow-up items
        - connect_impact: List of impact signal strings (if extract_impact was True)
    """
    date_context = f"on {date_str}" if date_str else ""
    
    # Clean title to avoid CLI argument parsing issues
    clean_title = _clean_meeting_title(meeting_title)
    # Remove any quotes that could break CLI argument parsing
    clean_title = clean_title.replace('"', '').replace("'", '')
    
    # Build prompt from template (custom or default)
    stripped_prompt = custom_prompt.strip() if custom_prompt else ''
    prompt_template = stripped_prompt if stripped_prompt else DEFAULT_SUMMARY_PROMPT
    try:
        question = prompt_template.format(title=clean_title, date=date_context)
    except (KeyError, IndexError):
        # If custom prompt has bad placeholders, fall back to default
        logger.warning(f"Custom prompt had invalid placeholders, using default")
        question = DEFAULT_SUMMARY_PROMPT.format(title=clean_title, date=date_context)
    
    # Always append task suggestion instructions (not user-editable)
    question += _TASK_PROMPT_SUFFIX
    
    # Conditionally append Connect impact extraction
    if extract_impact:
        question += _CONNECT_IMPACT_SUFFIX
    
    try:
        response = query_workiq(question, timeout=120)
        return _parse_summary_response(response)
    except TimeoutError:
        logger.warning(f"Meeting summary timed out for: {meeting_title}")
        return {
            'summary': "Summary request timed out. The meeting may not have a transcript, or the transcript is still processing.",
            'topics': [],
            'action_items': [],
            'task_subject': '',
            'task_description': '',
            'connect_impact': []
        }
    except Exception as e:
        logger.error(f"Failed to get meeting summary: {e}")
        return {
            'summary': f"Error fetching summary: {str(e)}",
            'topics': [],
            'action_items': [],
            'task_subject': '',
            'task_description': '',
            'connect_impact': []
        }


def _parse_summary_response(response: str) -> Dict[str, Any]:
    """Parse WorkIQ summary response into structured data.
    
    Handles both structured format (SUMMARY:/TECHNOLOGIES:/ACTION_ITEMS:) 
    and natural language responses.
    """
    result = {
        'summary': '',
        'topics': [],
        'action_items': [],
        'task_subject': '',
        'task_description': '',
        'connect_impact': [],
        'raw_response': response
    }
    
    # Extract CONNECT_IMPACT block before other parsing
    # Matches "CONNECT_IMPACT:" followed by dash-prefixed lines
    impact_match = re.search(
        r'CONNECT[_\s]IMPACT:\s*\n((?:\s*[-*]\s*.+\n?)+)',
        response, re.IGNORECASE
    )
    if impact_match:
        impact_lines = impact_match.group(1).strip().split('\n')
        result['connect_impact'] = [
            re.sub(r'^\s*[-*]\s*', '', line).strip()
            for line in impact_lines
            if line.strip() and re.sub(r'^\s*[-*]\s*', '', line).strip()
        ]
    
    # Remove CONNECT_IMPACT block from response before further parsing
    response = re.sub(
        r'CONNECT[_\s]IMPACT:\s*\n(?:\s*[-*]\s*.+\n?)+',
        '', response, flags=re.IGNORECASE
    ).strip()
    
    # Extract TASK_TITLE and TASK_DESCRIPTION from response (works for both formats)
    task_title_match = re.search(
        r'TASK[_\s]TITLE:\s*(.+?)(?:\n|$)', response, re.IGNORECASE
    )
    task_desc_match = re.search(
        r'TASK[_\s]DESCRIPTION:\s*(.+?)(?:\n|$)', response, re.IGNORECASE
    )
    if task_title_match:
        result['task_subject'] = task_title_match.group(1).strip().strip('"\'*')
    if task_desc_match:
        result['task_description'] = task_desc_match.group(1).strip().strip('"\'*')
    
    # Remove TASK_TITLE/TASK_DESCRIPTION lines from response before further parsing
    # so they don't pollute the summary text
    cleaned_response = re.sub(
        r'TASK[_\s](?:TITLE|DESCRIPTION):\s*.+?(?:\n|$)', '', response, flags=re.IGNORECASE
    ).strip()
    
    # Check if response has structured format
    has_structured = 'SUMMARY:' in cleaned_response or 'TECHNOLOGIES:' in cleaned_response
    
    if not has_structured:
        # Use natural language parsing
        # First, strip conversational AI preamble and follow-up offers
        cleaned_response = _clean_ai_preamble(cleaned_response)
        
        # Remove citation-style markdown links [1](url), [2](url) entirely
        clean_response = re.sub(r'\s*\[\d+\]\([^\)]+\)', '', cleaned_response)
        # Remove plain citation references [1], [2] that have no URL
        clean_response = re.sub(r'\s*\[\d+\]', '', clean_response)
        # Remove remaining markdown links [text](url) but keep the text
        clean_response = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', clean_response)
        
        # Remove heading markers
        clean_response = re.sub(r'^#{1,6}\s*', '', clean_response, flags=re.MULTILINE)
        
        # The whole response is the summary  
        result['summary'] = clean_response.strip()
        
        # Try to extract technologies mentioned (common Azure terms)
        azure_terms = re.findall(
            r'\b(Azure[A-Za-z\s]*|Microsoft Fabric|Power BI|SQL Server|Cosmos DB|'
            r'Synapse|Databricks|Data Factory|Logic Apps|Functions|'
            r'App Service|AKS|Kubernetes|Storage|Machine Learning|'
            r'Cognitive Services|OpenAI|AI|ML|ETL|Data Lake|'
            r'DevOps|GitHub|Event Hub|Service Bus|API Management)\b',
            cleaned_response, re.IGNORECASE
        )
        if azure_terms:
            # Dedupe while preserving order
            seen = set()
            result['topics'] = [t for t in azure_terms if not (t.lower() in seen or seen.add(t.lower()))][:10]
        
        # Try to extract action items (look for "next steps" patterns)
        action_pattern = r'(?:next steps?|action items?|follow[- ]?ups?|to[- ]?do)[:\s]*(?:\n|$)((?:[-•*\d\.]+\s*.+\n?)+)'
        action_match = re.search(action_pattern, cleaned_response, re.IGNORECASE)
        if action_match:
            items_text = action_match.group(1)
            items = re.findall(r'[-•*\d\.]+\s*(.+)', items_text)
            result['action_items'] = [item.strip() for item in items if item.strip()][:10]
        
        return result
    
    # Original structured parsing
    lines = cleaned_response.split('\n')
    current_section = None
    current_content = []
    
    for line in lines:
        stripped = line.strip()
        
        if stripped.startswith('SUMMARY:'):
            if current_section:
                _save_section(result, current_section, current_content)
            current_section = 'summary'
            content = stripped.replace('SUMMARY:', '').strip()
            current_content = [content] if content else []
            
        elif stripped.startswith('TECHNOLOGIES:'):
            if current_section:
                _save_section(result, current_section, current_content)
            current_section = 'technologies'
            content = stripped.replace('TECHNOLOGIES:', '').strip()
            current_content = [content] if content else []
            
        elif stripped.startswith('ACTION_ITEMS:') or stripped.startswith('ACTION ITEMS:'):
            if current_section:
                _save_section(result, current_section, current_content)
            current_section = 'action_items'
            content = stripped.replace('ACTION_ITEMS:', '').replace('ACTION ITEMS:', '').strip()
            current_content = [content] if content else []
            
        elif current_section and stripped:
            current_content.append(stripped)
    
    if current_section:
        _save_section(result, current_section, current_content)
    
    return result


def _save_section(result: Dict, section: str, content: List[str]):
    """Save parsed section content to result dict."""
    if section == 'summary':
        result['summary'] = ' '.join(content).strip()
    elif section == 'technologies':
        # Parse comma-separated list
        all_text = ' '.join(content)
        topics = [t.strip() for t in all_text.split(',') if t.strip()]
        # Clean up topics
        topics = [re.sub(r'^[-•*]\s*', '', t) for t in topics]
        result['topics'] = topics
    elif section == 'action_items':
        # Parse numbered/bulleted list
        items = []
        for line in content:
            # Remove numbering/bullets
            cleaned = re.sub(r'^[\d\.\)\-•*]+\s*', '', line.strip())
            if cleaned:
                items.append(cleaned)
        result['action_items'] = items
