"""
Revenue Import Service
=====================
Handles importing MSXI revenue CSV data into the database.
Stores ALL customer data (not just flagged ones) to build historical records.

CSV Format (from MSXI ACR Details by Quarter Month SL4):
Row 0: FiscalMonth, , , FY26-Jul, FY26-Aug, ..., Total
Row 1: TPAccountName, ServiceCompGrouping, ServiceLevel4, $ ACR, $ ACR, ...
Row 2+: CustomerName, Bucket, Product, $revenue, $revenue, ...

Where:
- Bucket = "Total" means customer total across all buckets
- Bucket = "Analytics"/"Core DBs"/etc with Product = "Total" means bucket total
- Bucket = "Analytics"/"Core DBs"/etc with Product = specific name means product detail

Fiscal Month Format: "FY26-Jan" where FY26 = July 2025 - June 2026
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Any
from io import StringIO, BytesIO

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    pd = None  # type: ignore
    HAS_PANDAS = False

from app.models import (
    db, RevenueImport, CustomerRevenueData, ProductRevenueData, Customer,
    SyncStatus
)


# Product consolidation rules - products starting with these prefixes get rolled up
PRODUCT_CONSOLIDATION_PREFIXES = [
    'Azure Synapse Analytics',
]

# Words too short or generic to match on when dropping trailing words
_SKIP_WORDS = {
    'a', 'an', 'the', 'of', 'and', 'for', 'in', 'at', 'by', 'to',
    'american', 'national', 'global', 'united', 'general', 'international',
}

# Minimum number of words required for a prefix match
_MIN_PREFIX_WORDS = 1

# Minimum character length for a single-word prefix match
_MIN_PREFIX_LEN = 4


def _clean_for_matching(name: str) -> str:
    """Clean a company name for prefix matching.

    Lowercases, strips punctuation (commas, periods, etc.), and collapses
    whitespace so "Azara Healthcare, LLC" becomes "azara healthcare llc".

    Args:
        name: Raw company name

    Returns:
        Cleaned lowercase string with punctuation removed
    """
    cleaned = re.sub(r'[^\w\s]', '', name.lower())
    return ' '.join(cleaned.split())


def _progressive_word_prefix_match(
    name_a: str,
    name_b: str,
) -> bool:
    """Check if one name's words match the leading words of the other,
    progressively dropping trailing words.

    Works at the WORD level, not character level. So "streamline health"
    does NOT match "streamline healthcare" on the first try (because
    "health" != "healthcare"), but after dropping "health", "streamline"
    matches the first word of "streamline healthcare solutions".

    Args:
        name_a: First cleaned name (from _clean_for_matching)
        name_b: Second cleaned name (from _clean_for_matching)

    Returns:
        True if a progressive word-level prefix match is found
    """
    if not name_a or not name_b:
        return False

    words_a = name_a.split()
    words_b = name_b.split()

    # Try both directions
    for src_words, tgt_words in [(words_a, words_b), (words_b, words_a)]:
        # Progressively drop trailing words from src and check if
        # the remaining words match the leading words of tgt exactly
        candidate = list(src_words)
        while candidate:
            if len(candidate) <= len(tgt_words):
                # Check if candidate words match the first N words of target
                if candidate == tgt_words[:len(candidate)]:
                    # Ensure the match is meaningful
                    match_str = ' '.join(candidate)
                    if len(candidate) >= 2 or len(match_str) >= _MIN_PREFIX_LEN:
                        return True
            candidate.pop()
            # Skip trailing stop words
            while candidate and candidate[-1] in _SKIP_WORDS:
                candidate.pop()

    return False


def _get_acronym(name: str) -> str:
    """Get the acronym from a company name (first letter of each word).

    Only uses words that are 2+ chars and not stop words.
    E.g., "Facilities Survey Inc" -> "FSI"

    Args:
        name: Company name

    Returns:
        Uppercase acronym string, or empty string if too short
    """
    cleaned = re.sub(r'[^\w\s]', '', name)
    words = [w for w in cleaned.split() if len(w) >= 2 and w.lower() not in _SKIP_WORDS]
    return ''.join(w[0] for w in words).upper()


def _build_customer_lookup() -> tuple[
    dict[str, int],
    list[tuple[str, int]],
    dict[str, int],
]:
    """Build customer lookup structures for matching revenue data to customers.

    Returns:
        Tuple of:
        - Quick lookup dict: lowercased exact name/nickname -> customer ID
        - Cleaned names list: (cleaned_name, customer_id) for prefix matching
        - Acronym lookup: uppercase acronym -> customer ID (for names/nicknames
          that are 2-6 chars and look like acronyms)
    """
    exact_lookup: dict[str, int] = {}
    cleaned_names: list[tuple[str, int]] = []
    acronym_lookup: dict[str, int] = {}

    for c in Customer.query.all():
        exact_lookup[c.name.lower()] = c.id
        cleaned_names.append((_clean_for_matching(c.name), c.id))
        # Register acronym of long customer names so CSV short names can match
        # e.g., customer "Facilities Survey Inc" -> acronym "FSI"
        name_acronym = _get_acronym(c.name)
        if len(name_acronym) >= 2 and name_acronym not in acronym_lookup:
            acronym_lookup[name_acronym] = c.id
        # If customer name itself looks like an acronym (2-6 chars, all alpha),
        # register it so revenue names can match by their acronym
        stripped = c.name.strip()
        if 2 <= len(stripped) <= 6 and stripped.isalpha():
            acronym_lookup[stripped.upper()] = c.id
        if c.nickname:
            exact_lookup[c.nickname.lower()] = c.id
            cleaned_names.append((_clean_for_matching(c.nickname), c.id))
            nick_acronym = _get_acronym(c.nickname)
            if len(nick_acronym) >= 2 and nick_acronym not in acronym_lookup:
                acronym_lookup[nick_acronym] = c.id
            nick_stripped = c.nickname.strip()
            if 2 <= len(nick_stripped) <= 6 and nick_stripped.isalpha():
                acronym_lookup[nick_stripped.upper()] = c.id

    return exact_lookup, cleaned_names, acronym_lookup


def _resolve_customer_id(
    exact_lookup: dict[str, int],
    cleaned_names: list[tuple[str, int]],
    customer_name: str,
    acronym_lookup: dict[str, int] | None = None,
) -> int | None:
    """Look up a customer ID using exact match, prefix match, then acronym.

    Matching tiers:
    1. Exact match on name or nickname (case-insensitive)
    2. Progressive word-prefix matching (drop trailing words)
    3. Acronym matching ("Facilities Survey Inc" -> "FSI")

    Args:
        exact_lookup: Dict from _build_customer_lookup() for exact matches
        cleaned_names: List from _build_customer_lookup() for prefix matches
        customer_name: Raw customer name from revenue data
        acronym_lookup: Optional dict from _build_customer_lookup() for acronyms

    Returns:
        Customer ID if found, None otherwise
    """
    # Tier 1: Exact match on name or nickname (case-insensitive)
    result = exact_lookup.get(customer_name.lower())
    if result is not None:
        return result

    # Tier 2: Progressive prefix matching
    cleaned = _clean_for_matching(customer_name)
    if len(cleaned) >= _MIN_PREFIX_LEN:
        for cust_cleaned, cust_id in cleaned_names:
            if _progressive_word_prefix_match(cleaned, cust_cleaned):
                return cust_id

    # Tier 3: Acronym matching
    # Direction A: Revenue name is long, customer name is short acronym
    #   e.g., CSV "Facilities Survey Inc" -> acronym "FSI" -> matches customer "FSI"
    # Direction B: Revenue name is short acronym, customer name is long
    #   e.g., CSV "FSI" -> direct lookup -> matches acronym of "Facilities Survey Inc"
    if acronym_lookup:
        # Direction A: compute acronym of the CSV name
        acronym = _get_acronym(customer_name)
        if len(acronym) >= 2:
            result = acronym_lookup.get(acronym)
            if result is not None:
                return result
        # Direction B: CSV name itself might be an acronym — look it up directly
        stripped = customer_name.strip()
        if 2 <= len(stripped) <= 6 and stripped.isalpha():
            result = acronym_lookup.get(stripped.upper())
            if result is not None:
                return result

    return None


def consolidate_product_name(product: str) -> str:
    """Get the consolidated product name for display purposes.
    
    Products starting with certain prefixes (like 'Azure Synapse Analytics')
    get consolidated into a single display name.
    
    Args:
        product: Original product name
        
    Returns:
        Consolidated product name (or original if no consolidation applies)
    """
    for prefix in PRODUCT_CONSOLIDATION_PREFIXES:
        if product.startswith(prefix):
            return prefix
    return product


def consolidate_products_list(products: list[dict]) -> list[dict]:
    """Consolidate a list of product dicts by rolling up matching prefixes.
    
    Products starting with consolidation prefixes get merged into a single entry
    with summed revenues and customer counts.
    
    Args:
        products: List of dicts with 'product', 'customer_count', 'total_revenue'
        
    Returns:
        Consolidated list with rolled-up products
    """
    consolidated = {}
    
    for p in products:
        display_name = consolidate_product_name(p['product'])
        
        if display_name not in consolidated:
            consolidated[display_name] = {
                'product': display_name,
                'customer_count': 0,
                'total_revenue': 0,
                '_original_products': []
            }
        
        # For customer count, we need to be careful not to double-count
        # if multiple sub-products have the same customer
        consolidated[display_name]['total_revenue'] += p.get('total_revenue', 0)
        consolidated[display_name]['_original_products'].append(p['product'])
        # Note: customer_count may over-count if same customer uses multiple sub-products
        # For now, we'll use the max of the individual counts as a rough estimate
        consolidated[display_name]['customer_count'] = max(
            consolidated[display_name]['customer_count'],
            p.get('customer_count', 0)
        )
    
    return list(consolidated.values())


class RevenueImportError(Exception):
    """Raised when import fails."""
    pass


def parse_currency(value) -> float:
    """Parse currency string to float.
    
    Handles: "$1,234.56", "$1234", "1234.56", etc.
    """
    if pd.isna(value) or value is None:
        return 0.0
    
    if isinstance(value, (int, float)):
        return float(value)
    
    # Remove currency symbols, commas, spaces
    cleaned = re.sub(r'[$,\s]', '', str(value))
    
    # Handle parentheses for negative numbers
    if cleaned.startswith('(') and cleaned.endswith(')'):
        cleaned = '-' + cleaned[1:-1]
    
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def fiscal_month_to_date(fiscal_month: str) -> Optional[date]:
    """Convert fiscal month string to date (first of that month).
    
    Microsoft FY runs July-June:
    - FY26-Jul = July 2025 (FY26 starts July 2025)
    - FY26-Jan = January 2026
    - FY26-Jun = June 2026 (last month of FY26)
    
    Args:
        fiscal_month: String like "FY26-Jan"
        
    Returns:
        date object for first of that month, or None if invalid
    """
    match = re.match(r'FY(\d{2})-(\w{3})', fiscal_month)
    if not match:
        return None
    
    fy_num = int(match.group(1))
    month_abbr = match.group(2)
    
    # Map month abbreviation to number
    month_map = {
        'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
        'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
    }
    
    month_num = month_map.get(month_abbr)
    if not month_num:
        return None
    
    # Calculate calendar year
    # FY26 = July 2025 - June 2026
    # Jul-Dec are in year (FY - 1), Jan-Jun are in year FY
    base_year = 2000 + fy_num  # FY26 -> 2026
    
    if month_num >= 7:  # Jul-Dec
        calendar_year = base_year - 1
    else:  # Jan-Jun
        calendar_year = base_year
    
    return date(calendar_year, month_num, 1)


def date_to_fiscal_month(d: date) -> str:
    """Convert date to fiscal month string.
    
    Args:
        d: date object
        
    Returns:
        String like "FY26-Jan"
    """
    month_abbrs = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    
    # Calculate fiscal year
    # Jul-Dec: FY is year + 1
    # Jan-Jun: FY is year
    if d.month >= 7:
        fy = d.year + 1 - 2000  # 2025 July -> FY26
    else:
        fy = d.year - 2000  # 2026 January -> FY26
    
    return f"FY{fy:02d}-{month_abbrs[d.month - 1]}"


def load_csv(file_content: bytes | str, filename: str = "upload.csv") -> Any:
    """Load CSV content into a DataFrame.
    
    Handles various encodings common in MSXI exports.
    
    Args:
        file_content: Raw bytes or string content of the CSV
        filename: Original filename for error messages
        
    Returns:
        DataFrame with raw CSV data
    """
    if not HAS_PANDAS:
        raise RevenueImportError("pandas is required for revenue import. Install with: pip install pandas")
    
    # If bytes, try various encodings
    encodings = ['utf-8', 'cp1252', 'latin-1', 'iso-8859-1']
    
    if isinstance(file_content, bytes):
        for encoding in encodings:
            try:
                content = file_content.decode(encoding)
                return pd.read_csv(StringIO(content), header=None)
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        raise RevenueImportError(f"Could not read CSV with any supported encoding: {filename}")
    else:
        return pd.read_csv(StringIO(file_content), header=None)


def process_csv(df: Any) -> tuple[Any, list[str], dict[str, int]]:
    """Process raw DataFrame to extract structured data.
    
    New format (ACR Details by Quarter Month SL4):
    Row 0: FiscalMonth, , , FY26-Jul, FY26-Jul, FY26-Jul, FY26-Jul, FY26-Jul, FY26-Aug, ..., Total
    Row 1: TPAccountName, ServiceCompGrouping, ServiceLevel4, $ ACR, $ ACR MoM, $ Average Daily ACR, ...
    
    Each month has 5 columns of metrics. We only want the first ($ ACR).
    
    Args:
        df: Raw DataFrame (pandas) from CSV
        
    Returns:
        Tuple of (processed DataFrame, list of unique month names, dict mapping month -> column index)
    """
    # Get month and metric rows
    month_row = df.iloc[0].tolist()
    metric_row = df.iloc[1].tolist() if len(df) > 1 else []
    
    # Validate required columns exist in the header row
    REQUIRED_COLUMNS = ['TPAccountName', 'ServiceCompGrouping', 'ServiceLevel4']
    header_values = [str(v).strip() for v in metric_row[:10] if pd.notna(v)]
    missing = [col for col in REQUIRED_COLUMNS if col not in header_values]
    if missing:
        raise RevenueImportError(
            f"This doesn't look like the right CSV. "
            f"Missing required columns: {', '.join(missing)}. "
            f"Make sure you're exporting from the 'ACR Details by Quarter / Month' table "
            f"with ServiceLevel4 checked under Choose Fields."
        )
    
    # Find where month columns start (look for FY pattern)
    month_start_idx = None
    for i, val in enumerate(month_row):
        if pd.notna(val) and 'FY' in str(val):
            month_start_idx = i
            break
    
    if month_start_idx is None:
        raise RevenueImportError("No fiscal month columns found (expecting FYxx-Mon format)")
    
    # Build a mapping of unique months to their $ ACR column index
    # Each month appears 5 times (5 metrics), we want the first occurrence ($ ACR)
    month_to_col_idx = {}  # month name -> column index
    unique_months = []  # ordered list of unique months
    
    for i, val in enumerate(month_row[month_start_idx:], start=month_start_idx):
        if pd.notna(val):
            val_str = str(val).strip()
            if val_str.lower() == 'total':
                break  # Stop at Total column
            if 'FY' in val_str:
                # Check if this is the first occurrence ($ ACR column)
                # The metric row should say "$ ACR" for the first column of each month group
                metric_label = str(metric_row[i]).strip() if i < len(metric_row) else ''
                
                if val_str not in month_to_col_idx:
                    # First occurrence of this month - this is the $ ACR column
                    month_to_col_idx[val_str] = i
                    unique_months.append(val_str)
    
    if not unique_months:
        raise RevenueImportError("No fiscal month columns found")
    
    # Build new column names - use positional indexing instead of named columns
    # to avoid ambiguous column name issues
    num_cols = len(df.columns)
    col_names = [f'col_{i}' for i in range(num_cols)]
    col_names[0] = 'TPAccountName'
    col_names[1] = 'ServiceCompGrouping'
    col_names[2] = 'ServiceLevel4'
    
    df.columns = col_names
    
    # Skip header rows (row 0 = month names, row 1 = column labels)
    df = df.iloc[2:].copy()
    
    # Filter out empty rows
    df = df[df['TPAccountName'].notna()]
    df = df[df['TPAccountName'] != '']
    df = df.reset_index(drop=True)
    
    return df, unique_months, month_to_col_idx


def _build_revenue_lookups(month_dates_values):
    """Pre-fetch existing revenue records into lookup dicts for bulk upsert.

    Instead of issuing one SELECT per row (N+1 pattern), this loads all
    existing records for the relevant months in two queries and indexes
    them by their natural-key tuples for O(1) in-memory lookups.

    Args:
        month_dates_values: Collection of date objects to filter by.

    Returns:
        Tuple of (customer_rev_lookup, product_rev_lookup, existing_month_dates)
        - customer_rev_lookup: dict mapping (customer_name, bucket, month_date)
          to the CustomerRevenueData instance
        - product_rev_lookup: dict mapping (customer_name, bucket, product,
          month_date) to the ProductRevenueData instance
        - existing_month_dates: set of month_date values already stored
    """
    dates_list = list(month_dates_values)

    customer_records = CustomerRevenueData.query.filter(
        CustomerRevenueData.month_date.in_(dates_list)
    ).all()
    customer_rev_lookup = {
        (r.customer_name, r.bucket, r.month_date): r
        for r in customer_records
    }

    product_records = ProductRevenueData.query.filter(
        ProductRevenueData.month_date.in_(dates_list)
    ).all()
    product_rev_lookup = {
        (r.customer_name, r.bucket, r.product, r.month_date): r
        for r in product_records
    }

    existing_month_rows = db.session.query(
        CustomerRevenueData.month_date
    ).distinct().all()
    existing_month_dates = {row[0] for row in existing_month_rows}

    return customer_rev_lookup, product_rev_lookup, existing_month_dates


def import_revenue_csv(
    file_content: bytes | str,
    filename: str,
    territory_alignments: Optional[dict] = None
) -> RevenueImport:
    """Import revenue data from CSV into the database.
    
    This stores ALL customer/month data, not just flagged ones.
    Uses upsert logic: updates existing records, creates new ones.
    
    New format handles:
    - Bucket totals (ServiceLevel4 = "Total") -> CustomerRevenueData (for analysis)
    - Product details (ServiceLevel4 = product name) -> ProductRevenueData (for drill-down)
    - Customer totals (ServiceCompGrouping = "Total") -> skipped (can be calculated)
    
    Args:
        file_content: Raw CSV content
        filename: Original filename
        territory_alignments: Optional dict mapping (customer_name, bucket) -> seller_name
        
    Returns:
        RevenueImport record with import stats
    """
    SyncStatus.mark_started('revenue_import')

    # Load and process CSV
    df = load_csv(file_content, filename)
    df, month_cols, month_to_col_idx = process_csv(df)
    
    if df.empty:
        raise RevenueImportError("No data rows found in CSV")
    
    # Convert month columns to dates
    month_dates = {}
    for mc in month_cols:
        d = fiscal_month_to_date(mc)
        if d:
            month_dates[mc] = d
    
    if not month_dates:
        raise RevenueImportError("Could not parse any fiscal month columns")
    
    # Create import record
    earliest = min(month_dates.values())
    latest = max(month_dates.values())
    
    import_record = RevenueImport(
        filename=filename,
        record_count=len(df),
        earliest_month=earliest,
        latest_month=latest
    )
    db.session.add(import_record)
    db.session.flush()  # Get the ID
    
    # Track stats
    bucket_records_created = 0
    bucket_records_updated = 0
    product_records_created = 0
    product_records_updated = 0
    new_months = set()
    
    # Build customer lookup (exact + prefix + acronym matching)
    exact_lookup, cleaned_names, acronym_lookup = _build_customer_lookup()
    
    # Bulk pre-fetch existing revenue records (eliminates N+1 queries)
    customer_rev_lookup, product_rev_lookup, existing_month_dates = \
        _build_revenue_lookups(month_dates.values())
    
    # Process each row
    for _, row in df.iterrows():
        customer_name = str(row['TPAccountName']).strip()
        bucket = str(row['ServiceCompGrouping']).strip() if pd.notna(row.get('ServiceCompGrouping')) else ''
        product = str(row['ServiceLevel4']).strip() if pd.notna(row.get('ServiceLevel4')) else ''
        
        if not customer_name:
            continue
        
        # Skip customer total rows (bucket = "Total")
        if bucket.lower() == 'total':
            continue
        
        # Try to match to existing NoteHelper customer
        customer_id = _resolve_customer_id(exact_lookup, cleaned_names, customer_name, acronym_lookup)
        
        # Get seller from territory alignments if provided
        seller_name = None
        if territory_alignments:
            seller_name = territory_alignments.get((customer_name, bucket))
        
        # Process each month column using column index
        for month_col, month_date in month_dates.items():
            col_idx = month_to_col_idx.get(month_col)
            if col_idx is None:
                continue
            # Access by column index (col_N format)
            col_name = f'col_{col_idx}'
            revenue = parse_currency(row.get(col_name, 0))
            fiscal_month = month_col
            
            # Determine if this is a bucket total or product detail
            is_bucket_total = (product.lower() == 'total' or product == '')
            
            if is_bucket_total:
                # Store in CustomerRevenueData (for analysis)
                crev_key = (customer_name, bucket, month_date)
                existing = customer_rev_lookup.get(crev_key)
                
                if existing:
                    if existing.revenue != revenue:
                        existing.revenue = revenue
                        existing.last_updated_at = datetime.now(timezone.utc)
                        existing.last_import_id = import_record.id
                        bucket_records_updated += 1
                    if customer_id and not existing.customer_id:
                        existing.customer_id = customer_id
                    if seller_name and not existing.seller_name:
                        existing.seller_name = seller_name
                else:
                    new_record = CustomerRevenueData(
                        customer_name=customer_name,
                        bucket=bucket,
                        customer_id=customer_id,
                        seller_name=seller_name,
                        fiscal_month=fiscal_month,
                        month_date=month_date,
                        revenue=revenue,
                        last_import_id=import_record.id
                    )
                    db.session.add(new_record)
                    customer_rev_lookup[crev_key] = new_record
                    bucket_records_created += 1
                    
                    # Track new months (from pre-fetched set)
                    if month_date not in existing_month_dates:
                        new_months.add(month_date)
                        existing_month_dates.add(month_date)
            else:
                # Store in ProductRevenueData (for drill-down)
                prev_key = (customer_name, bucket, product, month_date)
                existing = product_rev_lookup.get(prev_key)
                
                if existing:
                    if existing.revenue != revenue:
                        existing.revenue = revenue
                        existing.last_updated_at = datetime.now(timezone.utc)
                        existing.last_import_id = import_record.id
                        product_records_updated += 1
                    if customer_id and not existing.customer_id:
                        existing.customer_id = customer_id
                else:
                    new_record = ProductRevenueData(
                        customer_name=customer_name,
                        bucket=bucket,
                        product=product,
                        customer_id=customer_id,
                        fiscal_month=fiscal_month,
                        month_date=month_date,
                        revenue=revenue,
                        last_import_id=import_record.id
                    )
                    db.session.add(new_record)
                    product_rev_lookup[prev_key] = new_record
                    product_records_created += 1
    
    # Update import stats
    import_record.records_created = bucket_records_created + product_records_created
    import_record.records_updated = bucket_records_updated + product_records_updated
    import_record.new_months_added = len(new_months)
    
    db.session.commit()

    total_records = import_record.records_created + import_record.records_updated
    SyncStatus.mark_completed(
        'revenue_import', success=True,
        items_synced=total_records,
        details=f'{import_record.records_created} created, {import_record.records_updated} updated'
    )
    
    return import_record


def import_revenue_csv_streaming(
    file_content: bytes | str,
    filename: str,
    territory_alignments: Optional[dict] = None
):
    """Import revenue data from CSV with streaming progress updates.
    
    Yields progress dicts and finally a completion dict with the result.
    
    Args:
        file_content: Raw CSV content
        filename: Original filename
        territory_alignments: Optional dict mapping (customer_name, bucket) -> seller_name
        
    Yields:
        Progress dicts: {"message": "..."} or {"error": "..."} or {"complete": True, "result": RevenueImport}
    """
    SyncStatus.mark_started('revenue_import')
    yield {"message": "Reading CSV file..."}
    
    # Load and process CSV
    df = load_csv(file_content, filename)
    df, month_cols, month_to_col_idx = process_csv(df)
    
    if df.empty:
        yield {"error": "No data rows found in CSV"}
        return
    
    yield {"message": f"Found {len(df)} rows with {len(month_cols)} months of data"}
    
    # Convert month columns to dates
    month_dates = {}
    for mc in month_cols:
        d = fiscal_month_to_date(mc)
        if d:
            month_dates[mc] = d
    
    if not month_dates:
        yield {"error": "Could not parse any fiscal month columns"}
        return
    
    yield {"message": f"Processing months: {', '.join(month_cols)}"}
    
    # Create import record
    earliest = min(month_dates.values())
    latest = max(month_dates.values())
    
    import_record = RevenueImport(
        filename=filename,
        record_count=len(df),
        earliest_month=earliest,
        latest_month=latest
    )
    db.session.add(import_record)
    db.session.flush()
    
    # Track stats
    bucket_records_created = 0
    bucket_records_updated = 0
    product_records_created = 0
    product_records_updated = 0
    new_months = set()
    
    # Build customer lookup (exact + prefix + acronym matching)
    yield {"message": "Loading customer database..."}
    exact_lookup, cleaned_names, acronym_lookup = _build_customer_lookup()
    
    # Bulk pre-fetch existing revenue records (eliminates N+1 queries)
    yield {"message": "Pre-loading existing revenue records..."}
    customer_rev_lookup, product_rev_lookup, existing_month_dates = \
        _build_revenue_lookups(month_dates.values())
    
    # Process rows with progress updates
    total_rows = len(df)
    last_progress = 0
    
    yield {"message": f"Processing {total_rows} revenue records...", "progress": 0}
    
    for idx, (_, row) in enumerate(df.iterrows()):
        # Update progress every 10% (use idx+1 since we calculate after starting to process)
        progress = int(((idx + 1) / total_rows) * 100)
        if progress >= last_progress + 10:
            yield {"message": f"Processing records... ({progress}%)", "progress": progress}
            last_progress = progress
        
        customer_name = str(row['TPAccountName']).strip()
        bucket = str(row['ServiceCompGrouping']).strip() if pd.notna(row.get('ServiceCompGrouping')) else ''
        product = str(row['ServiceLevel4']).strip() if pd.notna(row.get('ServiceLevel4')) else ''
        
        if not customer_name:
            continue
        
        # Skip customer total rows (bucket = "Total")
        if bucket.lower() == 'total':
            continue
        
        # Try to match to existing NoteHelper customer
        customer_id = _resolve_customer_id(exact_lookup, cleaned_names, customer_name, acronym_lookup)
        
        # Get seller from territory alignments if provided
        seller_name = None
        if territory_alignments:
            seller_name = territory_alignments.get((customer_name, bucket))
        
        # Process each month column using column index
        for month_col, month_date in month_dates.items():
            col_idx = month_to_col_idx.get(month_col)
            if col_idx is None:
                continue
            # Access by column index (col_N format)
            col_name = f'col_{col_idx}'
            revenue = parse_currency(row.get(col_name, 0))
            fiscal_month = month_col
            
            is_bucket_total = (product.lower() == 'total' or product == '')
            
            if is_bucket_total:
                crev_key = (customer_name, bucket, month_date)
                existing = customer_rev_lookup.get(crev_key)
                
                if existing:
                    if existing.revenue != revenue:
                        existing.revenue = revenue
                        existing.last_updated_at = datetime.now(timezone.utc)
                        existing.last_import_id = import_record.id
                        bucket_records_updated += 1
                    if customer_id and not existing.customer_id:
                        existing.customer_id = customer_id
                    if seller_name and not existing.seller_name:
                        existing.seller_name = seller_name
                else:
                    new_record = CustomerRevenueData(
                        customer_name=customer_name,
                        bucket=bucket,
                        customer_id=customer_id,
                        seller_name=seller_name,
                        fiscal_month=fiscal_month,
                        month_date=month_date,
                        revenue=revenue,
                        last_import_id=import_record.id
                    )
                    db.session.add(new_record)
                    customer_rev_lookup[crev_key] = new_record
                    bucket_records_created += 1
                    
                    if month_date not in existing_month_dates:
                        new_months.add(month_date)
                        existing_month_dates.add(month_date)
            else:
                prev_key = (customer_name, bucket, product, month_date)
                existing = product_rev_lookup.get(prev_key)
                
                if existing:
                    if existing.revenue != revenue:
                        existing.revenue = revenue
                        existing.last_updated_at = datetime.now(timezone.utc)
                        existing.last_import_id = import_record.id
                        product_records_updated += 1
                    if customer_id and not existing.customer_id:
                        existing.customer_id = customer_id
                else:
                    new_record = ProductRevenueData(
                        customer_name=customer_name,
                        bucket=bucket,
                        product=product,
                        customer_id=customer_id,
                        fiscal_month=fiscal_month,
                        month_date=month_date,
                        revenue=revenue,
                        last_import_id=import_record.id
                    )
                    db.session.add(new_record)
                    product_rev_lookup[prev_key] = new_record
                    product_records_created += 1
    
    yield {"message": "Saving to database...", "progress": 95}
    
    # Update import stats
    import_record.records_created = bucket_records_created + product_records_created
    import_record.records_updated = bucket_records_updated + product_records_updated
    import_record.new_months_added = len(new_months)
    
    db.session.commit()

    total_records = import_record.records_created + import_record.records_updated
    SyncStatus.mark_completed(
        'revenue_import', success=True,
        items_synced=total_records,
        details=f'{import_record.records_created} created, {import_record.records_updated} updated'
    )
    
    yield {"message": f"Import complete: {import_record.records_created} created, {import_record.records_updated} updated", "progress": 100}
    yield {"complete": True, "result": import_record}


def load_territory_alignments(file_content: bytes | str) -> dict:
    """Load territory alignments CSV to map customers to sellers.
    
    Expected format: CSV with columns including TPID, customer name, seller alias
    
    Args:
        file_content: Raw CSV content
        
    Returns:
        Dict mapping (customer_name, bucket) -> seller_name
    """
    if pd is None:
        return {}
    
    try:
        if isinstance(file_content, bytes):
            df = pd.read_csv(BytesIO(file_content))
        else:
            df = pd.read_csv(StringIO(file_content))
        
        alignments = {}
        # This is a placeholder - actual column names depend on the territory file format
        # We'll need to adapt this based on the actual file structure
        
        return alignments
    except Exception:
        return {}


def get_import_history(limit: int = 20) -> list[RevenueImport]:
    """Get recent import history.
    
    Args:
        limit: Max number of imports to return
        
    Returns:
        List of RevenueImport records, most recent first
    """
    return RevenueImport.query.order_by(
        RevenueImport.imported_at.desc()
    ).limit(limit).all()


def get_months_in_database() -> list[dict]:
    """Get all unique months in the database with record counts.
    
    Returns:
        List of dicts with month_date, fiscal_month, record_count
    """
    results = db.session.query(
        CustomerRevenueData.month_date,
        CustomerRevenueData.fiscal_month,
        db.func.count(CustomerRevenueData.id).label('record_count')
    ).group_by(
        CustomerRevenueData.month_date
    ).order_by(
        CustomerRevenueData.month_date.desc()
    ).all()
    
    return [
        {
            'month_date': r.month_date,
            'fiscal_month': r.fiscal_month,
            'record_count': r.record_count
        }
        for r in results
    ]


def get_customer_revenue_history(
    customer_name: Optional[str] = None,
    bucket: Optional[str] = None,
    *,
    customer_id: Optional[int] = None
) -> list[CustomerRevenueData]:
    """Get revenue history for a specific customer.
    
    Args:
        customer_name: Customer name to look up (from CSV)
        bucket: Optional bucket filter (Core DBs, Analytics, Modern DBs)
        customer_id: NoteHelper customer ID (preferred over customer_name)
        
    Returns:
        List of CustomerRevenueData records ordered by month
    """
    if customer_id:
        query = CustomerRevenueData.query.filter_by(customer_id=customer_id)
    elif customer_name:
        query = CustomerRevenueData.query.filter_by(customer_name=customer_name)
    else:
        return []
    
    if bucket:
        query = query.filter_by(bucket=bucket)
    
    return query.order_by(CustomerRevenueData.month_date).all()


def get_product_revenue_history(
    customer_name: Optional[str] = None,
    bucket: Optional[str] = None,
    product: Optional[str] = None,
    *,
    customer_id: Optional[int] = None
) -> list[ProductRevenueData]:
    """Get product-level revenue history for a customer/bucket.
    
    Args:
        customer_name: Customer name to look up (from CSV)
        bucket: Bucket name (Core DBs, Analytics, Modern DBs)
        product: Optional specific product filter
        customer_id: NoteHelper customer ID (preferred over customer_name)
        
    Returns:
        List of ProductRevenueData records ordered by product then month
    """
    if customer_id:
        query = ProductRevenueData.query.filter_by(customer_id=customer_id)
    elif customer_name:
        query = ProductRevenueData.query.filter_by(customer_name=customer_name)
    else:
        return []
    
    if bucket:
        query = query.filter_by(bucket=bucket)
    
    if product:
        query = query.filter_by(product=product)
    
    return query.order_by(
        ProductRevenueData.product,
        ProductRevenueData.month_date
    ).all()


def get_products_for_bucket(
    customer_name: Optional[str] = None,
    bucket: Optional[str] = None,
    *,
    customer_id: Optional[int] = None
) -> list[dict]:
    """Get all products used by a customer in a specific bucket with totals.
    
    Args:
        customer_name: Customer name (from CSV)
        bucket: Bucket name
        customer_id: NoteHelper customer ID (preferred over customer_name)
        
    Returns:
        List of dicts with product name and total revenue
    """
    filters = []
    if customer_id:
        filters.append(ProductRevenueData.customer_id == customer_id)
    elif customer_name:
        filters.append(ProductRevenueData.customer_name == customer_name)
    else:
        return []
    
    if bucket:
        filters.append(ProductRevenueData.bucket == bucket)
    
    results = db.session.query(
        ProductRevenueData.product,
        db.func.sum(ProductRevenueData.revenue).label('total_revenue'),
        db.func.count(ProductRevenueData.id).label('month_count')
    ).filter(
        *filters
    ).group_by(
        ProductRevenueData.product
    ).order_by(
        db.func.sum(ProductRevenueData.revenue).desc()
    ).all()
    
    return [
        {
            'product': r.product,
            'total_revenue': r.total_revenue or 0,
            'month_count': r.month_count
        }
        for r in results
    ]


def get_all_products() -> list[dict]:
    """Get all unique products in the database with usage stats.
    
    Returns:
        List of dicts with product name, customer count, total revenue
    """
    results = db.session.query(
        ProductRevenueData.product,
        db.func.count(db.distinct(ProductRevenueData.customer_name)).label('customer_count'),
        db.func.sum(ProductRevenueData.revenue).label('total_revenue')
    ).group_by(
        ProductRevenueData.product
    ).order_by(
        db.func.sum(ProductRevenueData.revenue).desc()
    ).all()
    
    return [
        {
            'product': r.product,
            'customer_count': r.customer_count,
            'total_revenue': r.total_revenue or 0
        }
        for r in results
    ]


def get_customers_using_product(product: str) -> list[dict]:
    """Get all customers using a specific product with their revenue history.
    
    Args:
        product: Product name to look up
        
    Returns:
        List of dicts with customer info and revenue data
    """
    results = db.session.query(
        ProductRevenueData.customer_name,
        ProductRevenueData.bucket,
        db.func.max(ProductRevenueData.customer_id).label('customer_id'),
        db.func.sum(ProductRevenueData.revenue).label('total_revenue'),
        db.func.max(ProductRevenueData.month_date).label('latest_month')
    ).filter_by(
        product=product
    ).group_by(
        ProductRevenueData.customer_name,
        ProductRevenueData.bucket
    ).having(
        db.func.sum(ProductRevenueData.revenue) > 0
    ).order_by(
        db.func.sum(ProductRevenueData.revenue).desc()
    ).all()
    
    return [
        {
            'customer_name': r.customer_name,
            'bucket': r.bucket,
            'customer_id': r.customer_id,
            'total_revenue': r.total_revenue or 0,
            'latest_month': r.latest_month
        }
        for r in results
    ]


def get_seller_products(seller_name: str) -> list[dict]:
    """Get all unique products used by a seller's customers.
    
    Args:
        seller_name: Seller name to filter by
        
    Returns:
        List of dicts with product name, customer count, total revenue
    """
    # Get customer names for this seller from analyses
    from app.models import RevenueAnalysis
    seller_customers = db.session.query(
        db.distinct(RevenueAnalysis.customer_name)
    ).filter_by(seller_name=seller_name).scalar_subquery()
    
    results = db.session.query(
        ProductRevenueData.product,
        db.func.count(db.distinct(ProductRevenueData.customer_name)).label('customer_count'),
        db.func.sum(ProductRevenueData.revenue).label('total_revenue')
    ).filter(
        ProductRevenueData.customer_name.in_(seller_customers)
    ).group_by(
        ProductRevenueData.product
    ).order_by(
        db.func.sum(ProductRevenueData.revenue).desc()
    ).all()
    
    return [
        {
            'product': r.product,
            'customer_count': r.customer_count,
            'total_revenue': r.total_revenue or 0
        }
        for r in results
    ]


def get_seller_customers_using_product(seller_name: str, product: str) -> list[dict]:
    """Get seller's customers using a specific product.
    
    Args:
        seller_name: Seller name to filter by
        product: Product name to look up
        
    Returns:
        List of dicts with customer info and revenue data
    """
    from app.models import RevenueAnalysis
    
    # Get customer names for this seller from analyses
    seller_customers = db.session.query(
        db.distinct(RevenueAnalysis.customer_name)
    ).filter_by(seller_name=seller_name).scalar_subquery()
    
    results = db.session.query(
        ProductRevenueData.customer_name,
        ProductRevenueData.bucket,
        db.func.max(ProductRevenueData.customer_id).label('customer_id'),
        db.func.sum(ProductRevenueData.revenue).label('total_revenue'),
        db.func.max(ProductRevenueData.month_date).label('latest_month')
    ).filter(
        ProductRevenueData.product == product,
        ProductRevenueData.customer_name.in_(seller_customers)
    ).group_by(
        ProductRevenueData.customer_name,
        ProductRevenueData.bucket
    ).order_by(
        db.func.sum(ProductRevenueData.revenue).desc()
    ).all()
    
    return [
        {
            'customer_name': r.customer_name,
            'bucket': r.bucket,
            'customer_id': r.customer_id,
            'total_revenue': r.total_revenue or 0,
            'latest_month': r.latest_month
        }
        for r in results
    ]


def get_new_product_users(consolidated_product: str, months_lookback: int = 6) -> list[dict]:
    """Find customers who recently started using a consolidated product.
    
    A customer is a "new user" if:
    - They have at least one non-zero month for the product
    - Their first non-zero month is within the lookback period
    - (Meaning they had $0 in all months before that)
    
    Args:
        consolidated_product: The consolidated product name (e.g., 'Azure Synapse Analytics')
        months_lookback: How many recent months to consider as "recently started"
        
    Returns:
        List of dicts with customer info, seller, first usage month, and current usage
    """
    from app.models import RevenueAnalysis
    
    # Get the recent months in the database (sorted chronologically - newest first from query)
    months = get_months_in_database()
    if not months:
        return []
    
    # Reverse to get chronological order (oldest first) for easier reasoning
    months_chrono = list(reversed(months))
    
    # Get the lookback threshold - we want customers whose first usage is within the last N months
    # If we have 7 months and want 6 months lookback, we exclude only month[0] (the oldest)
    if len(months_chrono) > months_lookback:
        oldest_allowed_first_usage = months_chrono[-months_lookback]['month_date']
    else:
        # Not enough history - include everyone
        oldest_allowed_first_usage = months_chrono[0]['month_date']
    
    # Find all products that belong to this consolidated group
    matching_products = []
    for prefix in PRODUCT_CONSOLIDATION_PREFIXES:
        if consolidated_product == prefix:
            # Get all products starting with this prefix
            product_rows = db.session.query(
                db.distinct(ProductRevenueData.product)
            ).filter(
                ProductRevenueData.product.like(f"{prefix}%")
            ).all()
            matching_products.extend([p[0] for p in product_rows])
            break
    
    if not matching_products:
        # Not a consolidated product, just use exact match
        matching_products = [consolidated_product]
    
    # For each customer, find their first month with non-zero revenue for these products
    # Subquery to get first usage month per customer
    first_usage_subq = db.session.query(
        ProductRevenueData.customer_name,
        db.func.min(ProductRevenueData.month_date).label('first_usage_date')
    ).filter(
        ProductRevenueData.product.in_(matching_products),
        ProductRevenueData.revenue > 0
    ).group_by(
        ProductRevenueData.customer_name
    ).subquery()
    
    # Get customers whose first usage is within the lookback period
    new_users_query = db.session.query(
        first_usage_subq.c.customer_name,
        first_usage_subq.c.first_usage_date
    ).filter(
        first_usage_subq.c.first_usage_date >= oldest_allowed_first_usage
    ).all()
    
    if not new_users_query:
        return []
    
    new_user_names = {r.customer_name: r.first_usage_date for r in new_users_query}
    
    # Get customer_id mapping from revenue data (set during import)
    customer_id_map = dict(
        db.session.query(
            ProductRevenueData.customer_name,
            db.func.max(ProductRevenueData.customer_id)
        ).filter(
            ProductRevenueData.product.in_(matching_products),
            ProductRevenueData.customer_id.isnot(None)
        ).group_by(
            ProductRevenueData.customer_name
        ).all()
    )
    
    # Get seller info and total revenue for these customers
    results = []
    for customer_name, first_usage_date in new_user_names.items():
        # Get seller from RevenueAnalysis
        analysis = RevenueAnalysis.query.filter_by(customer_name=customer_name).first()
        seller_name = analysis.seller_name if analysis else None
        
        # Get total revenue for this customer on these products
        total_rev = db.session.query(
            db.func.sum(ProductRevenueData.revenue)
        ).filter(
            ProductRevenueData.customer_name == customer_name,
            ProductRevenueData.product.in_(matching_products)
        ).scalar() or 0
        
        # Get the most recent month's revenue (months_chrono[-1] is newest)
        latest_month = months_chrono[-1]['month_date']
        latest_rev = db.session.query(
            db.func.sum(ProductRevenueData.revenue)
        ).filter(
            ProductRevenueData.customer_name == customer_name,
            ProductRevenueData.product.in_(matching_products),
            ProductRevenueData.month_date == latest_month
        ).scalar() or 0
        
        # Get the fiscal month string for the first usage
        first_usage_fiscal = None
        for m in months_chrono:
            if m['month_date'] == first_usage_date:
                first_usage_fiscal = m['fiscal_month']
                break
        
        results.append({
            'customer_name': customer_name,
            'seller_name': seller_name,
            'first_usage_date': first_usage_date,
            'first_usage_fiscal': first_usage_fiscal,
            'total_revenue': total_rev,
            'latest_month_revenue': latest_rev,
            'customer_id': customer_id_map.get(customer_name)
        })
    
    # Sort by seller name (None last), then by customer name
    results.sort(key=lambda x: (x['seller_name'] is None, x['seller_name'] or '', x['customer_name']))
    
    return results