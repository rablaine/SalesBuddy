"""
Revenue Analysis Service
========================
Computes revenue health signals and categorizes customers.
Port of the standalone revenue-analyzer logic.

Signal Detection:
- Trend slope (linear regression)
- Recent momentum (last 1-2 month changes)
- Volatility (coefficient of variation)
- Level relative to history

Categories:
- CHURN_RISK: Declining trend + recent drop
- RECENT_DIP: Stable/positive trend but recent sharp drop  
- EXPANSION_OPPORTUNITY: Positive slope + acceleration + near max
- VOLATILE: High instability in usage
- STAGNANT: Flat, low variance
- HEALTHY: No concerning signals
- NEW_CUSTOMER: Started generating revenue recently
- CHURNED: Revenue dropped to zero
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field

from app.models import (
    db, CustomerRevenueData, RevenueAnalysis, RevenueConfig, Customer,
    SyncStatus
)


# =============================================================================
# CONFIGURATION - Default thresholds (can be overridden by RevenueConfig)
# =============================================================================

@dataclass
class AnalysisConfig:
    """Configuration thresholds for revenue analysis."""
    
    # Revenue gates
    min_revenue_for_outreach: int = 3000
    min_dollar_impact: int = 1000
    dollar_at_risk_override: int = 2000
    dollar_opportunity_override: int = 1500
    
    # Revenue tiers
    high_value_threshold: int = 25000
    strategic_threshold: int = 50000
    
    # Category thresholds
    volatile_min_revenue: int = 5000
    recent_drop_threshold: float = -0.15
    expansion_growth_threshold: float = 0.08
    
    # Low confidence multiplier
    low_confidence_revenue_multiplier: float = 2.0
    
    @classmethod
    def from_db(cls) -> "AnalysisConfig":
        """Load config from database or return defaults."""
        db_config = RevenueConfig.query.first()
        if not db_config:
            return cls()
        
        return cls(
            min_revenue_for_outreach=db_config.min_revenue_for_outreach,
            min_dollar_impact=db_config.min_dollar_impact,
            dollar_at_risk_override=db_config.dollar_at_risk_override,
            dollar_opportunity_override=db_config.dollar_opportunity_override,
            high_value_threshold=db_config.high_value_threshold,
            strategic_threshold=db_config.strategic_threshold,
            volatile_min_revenue=db_config.volatile_min_revenue,
            recent_drop_threshold=db_config.recent_drop_threshold,
            expansion_growth_threshold=db_config.expansion_growth_threshold,
        )


# =============================================================================
# SIGNAL COMPUTATION
# =============================================================================

@dataclass
class CustomerSignals:
    """Statistical signals computed for a customer/bucket."""
    
    customer_name: str
    bucket: str
    revenues: list[float]
    month_names: list[str]
    
    # Optional identifiers
    tpid: Optional[str] = None
    seller_name: Optional[str] = None
    customer_id: Optional[int] = None
    
    # Computed signals
    avg_revenue: float = 0.0
    trend_slope: float = 0.0  # %/month
    trend_r_squared: float = 0.0
    
    # Month-over-month changes
    mom_changes: list[float] = field(default_factory=list)
    last_month_change: float = 0.0
    last_2month_change: float = 0.0
    
    # Volatility
    volatility_cv: float = 0.0
    max_drawdown: float = 0.0
    
    # Level relative to history
    current_vs_max: float = 0.0
    current_vs_avg: float = 0.0
    
    # Category results
    category: str = "HEALTHY"
    confidence: str = "MEDIUM"
    reason: str = ""
    
    # Recommendation results
    recommended_action: str = "NO ACTION"
    engagement_rationale: str = ""
    priority_score: int = 0
    dollars_at_risk: float = 0.0
    dollars_opportunity: float = 0.0


def compute_linear_regression(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """
    Compute simple linear regression.
    Returns: (slope, intercept, r_squared)
    """
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0
    
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    
    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    denominator = sum((x[i] - mean_x) ** 2 for i in range(n))
    
    if denominator == 0:
        return 0.0, mean_y, 0.0
    
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    
    # R-squared
    y_pred = [slope * xi + intercept for xi in x]
    ss_res = sum((y[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((y[i] - mean_y) ** 2 for i in range(n))
    
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    
    return slope, intercept, r_squared


def compute_signals(
    customer_name: str,
    bucket: str,
    revenues: list[float],
    month_names: list[str],
    tpid: Optional[str] = None,
    seller_name: Optional[str] = None,
    customer_id: Optional[int] = None
) -> Optional[CustomerSignals]:
    """
    Compute statistical signals for a customer/bucket.
    
    Args:
        customer_name: Customer name
        bucket: Product bucket (Core DBs, Analytics, Modern DBs)
        revenues: List of monthly revenue values (chronological order)
        month_names: List of fiscal month names matching revenues
        tpid: Optional TPID
        seller_name: Optional assigned seller
        customer_id: Optional NoteHelper customer ID
        
    Returns:
        CustomerSignals object, or None if insufficient data
    """
    # Skip if no meaningful revenue
    total_revenue = sum(revenues)
    if total_revenue < 500:
        return None
    
    n = len(revenues)
    if n < 3:
        return None
    
    signals = CustomerSignals(
        customer_name=customer_name,
        bucket=bucket,
        revenues=revenues,
        month_names=month_names,
        tpid=tpid,
        seller_name=seller_name,
        customer_id=customer_id
    )
    
    # Basic stats
    signals.avg_revenue = statistics.mean(revenues)
    non_zero_revenues = [r for r in revenues if r > 0]
    
    # Check for special cases
    zeros_at_start = 0
    for r in revenues:
        if r == 0:
            zeros_at_start += 1
        else:
            break
    
    zeros_at_end = 0
    for r in reversed(revenues):
        if r == 0:
            zeros_at_end += 1
        else:
            break
    
    # New customer: started with zeros
    if zeros_at_start >= 2 and len(non_zero_revenues) >= 2:
        signals.category = "NEW_CUSTOMER"
        signals.confidence = "HIGH"
        active_avg = statistics.mean(non_zero_revenues)
        signals.avg_revenue = active_avg
        signals.reason = f"Started generating revenue in {month_names[zeros_at_start]}. Active avg: ${active_avg:,.0f}"
        return signals
    
    # Churned: ended with zeros
    if zeros_at_end >= 2 and len(non_zero_revenues) >= 2:
        signals.category = "CHURNED"
        signals.confidence = "HIGH"
        previous_avg = statistics.mean(non_zero_revenues)
        signals.reason = f"Revenue dropped to $0. Previous avg: ${previous_avg:,.0f}"
        return signals
    
    # Need mostly non-zero values for remaining analysis
    if len(non_zero_revenues) < n * 0.6:
        return None
    
    # Trend slope (linear regression)
    x_values = list(range(1, n + 1))
    slope, intercept, r_squared = compute_linear_regression(x_values, revenues)
    
    if signals.avg_revenue > 0:
        signals.trend_slope = (slope / signals.avg_revenue) * 100
    signals.trend_r_squared = r_squared
    
    # Month-over-month changes
    mom_changes = []
    for i in range(1, n):
        if revenues[i-1] > 0:
            change = (revenues[i] - revenues[i-1]) / revenues[i-1]
            mom_changes.append(change)
        else:
            mom_changes.append(0.0)
    signals.mom_changes = mom_changes
    
    # Recent momentum
    if revenues[n-2] > 0:
        signals.last_month_change = (revenues[n-1] - revenues[n-2]) / revenues[n-2]
    
    if n >= 3 and revenues[n-3] > 0:
        signals.last_2month_change = (revenues[n-1] - revenues[n-3]) / revenues[n-3]
    
    # Volatility (CV of % changes)
    if len(mom_changes) >= 2:
        abs_changes = [abs(c) for c in mom_changes]
        mean_change = statistics.mean(abs_changes)
        if mean_change > 0:
            signals.volatility_cv = statistics.stdev(mom_changes) / mean_change
        else:
            signals.volatility_cv = statistics.stdev(mom_changes) if len(mom_changes) > 1 else 0
    
    # Max drawdown
    peak = revenues[0]
    max_dd = 0.0
    for r in revenues[1:]:
        if r > peak:
            peak = r
        elif peak > 0:
            dd = (peak - r) / peak
            max_dd = max(max_dd, dd)
    signals.max_drawdown = max_dd
    
    # Current vs history
    history = revenues[:-1]
    if history and max(history) > 0:
        signals.current_vs_max = revenues[-1] / max(history)
    if history and statistics.mean(history) > 0:
        signals.current_vs_avg = revenues[-1] / statistics.mean(history)
    
    # Categorize
    signals = categorize_customer(signals)
    
    return signals


def categorize_customer(signals: CustomerSignals) -> CustomerSignals:
    """Apply decision rules to categorize customer based on signals."""
    
    # Thresholds
    DECLINING_SLOPE = -5.0
    SHARP_DROP = -0.20
    MODERATE_DROP = -0.10
    GROWING_SLOPE = 5.0
    HIGH_VOLATILITY = 0.50
    STAGNANT_SLOPE = 2.0
    LOW_VOLATILITY = 0.20
    COLLAPSE_THRESHOLD = 0.70
    SPIKE_THRESHOLD = 1.30
    
    slope = signals.trend_slope
    last_change = signals.last_month_change
    last_2m_change = signals.last_2month_change
    volatility = signals.volatility_cv
    current_vs_max = signals.current_vs_max
    current_vs_avg = signals.current_vs_avg
    
    reasons = []
    
    # CHURN_RISK: Declining trend + recent drop
    if slope < DECLINING_SLOPE and last_change < SHARP_DROP:
        signals.category = "CHURN_RISK"
        signals.confidence = "HIGH"
        reasons.append(f"Declining {slope:+.1f}%/month")
        reasons.append(f"Last month dropped {last_change:+.1%}")
        signals.reason = " | ".join(reasons)
        return signals
    
    if slope < DECLINING_SLOPE and last_2m_change < SHARP_DROP:
        signals.category = "CHURN_RISK"
        signals.confidence = "MEDIUM"
        reasons.append(f"Declining {slope:+.1f}%/month")
        reasons.append(f"Down {last_2m_change:+.1%} over 2 months")
        signals.reason = " | ".join(reasons)
        return signals
    
    # Collapse check
    if current_vs_max < COLLAPSE_THRESHOLD and slope < 0:
        signals.category = "CHURN_RISK"
        signals.confidence = "MEDIUM"
        reasons.append(f"Revenue at {current_vs_max:.0%} of peak")
        reasons.append(f"Trend: {slope:+.1f}%/month")
        signals.reason = " | ".join(reasons)
        return signals
    
    # RECENT_DIP: Stable trend but sharp recent drop
    if slope >= DECLINING_SLOPE and last_change < SHARP_DROP:
        signals.category = "RECENT_DIP"
        signals.confidence = "HIGH"
        reasons.append(f"Overall trend stable ({slope:+.1f}%/month)")
        reasons.append(f"But last month dropped {last_change:+.1%}")
        signals.reason = " | ".join(reasons)
        return signals
    
    if slope >= 0 and last_2m_change < MODERATE_DROP and last_change < 0:
        signals.category = "RECENT_DIP"
        signals.confidence = "MEDIUM"
        reasons.append(f"Positive trend ({slope:+.1f}%/month)")
        reasons.append(f"Recent softness: {last_2m_change:+.1%} over 2 months")
        signals.reason = " | ".join(reasons)
        return signals
    
    # EXPANSION_OPPORTUNITY: Positive slope + acceleration + near max
    if slope > GROWING_SLOPE:
        is_accelerating = last_change > 0.05
        near_max = current_vs_max > 0.90
        above_avg = current_vs_avg > SPIKE_THRESHOLD
        
        if is_accelerating or near_max or above_avg:
            signals.category = "EXPANSION_OPPORTUNITY"
            signals.confidence = "HIGH" if (is_accelerating and near_max) else "MEDIUM"
            reasons.append(f"Growing {slope:+.1f}%/month")
            if is_accelerating:
                reasons.append(f"Accelerating (+{last_change:.1%} last month)")
            if near_max:
                reasons.append(f"Near historical max ({current_vs_max:.0%})")
            if above_avg:
                reasons.append(f"Above avg ({current_vs_avg:.0%} of historical)")
            signals.reason = " | ".join(reasons)
            return signals
    
    # VOLATILE: High instability
    if volatility > HIGH_VOLATILITY:
        signals.category = "VOLATILE"
        signals.confidence = "MEDIUM"
        reasons.append(f"High volatility (CV: {volatility:.0%})")
        reasons.append(f"Max drawdown: {signals.max_drawdown:.0%}")
        if slope < 0:
            reasons.append(f"With downward trend ({slope:+.1f}%/month)")
            signals.confidence = "HIGH"
        signals.reason = " | ".join(reasons)
        return signals
    
    # STAGNANT: Flat, low variance
    if abs(slope) < STAGNANT_SLOPE and volatility < LOW_VOLATILITY:
        signals.category = "STAGNANT"
        signals.confidence = "LOW"
        reasons.append(f"Flat trend ({slope:+.1f}%/month)")
        reasons.append(f"Low volatility ({volatility:.0%})")
        signals.reason = " | ".join(reasons)
        return signals
    
    # HEALTHY: No concerning signals
    signals.category = "HEALTHY"
    signals.confidence = "LOW"
    reasons.append(f"Trend: {slope:+.1f}%/month")
    reasons.append(f"Volatility: {volatility:.0%}")
    signals.reason = " | ".join(reasons)
    
    return signals


def determine_action(signals: CustomerSignals, config: AnalysisConfig) -> CustomerSignals:
    """Determine recommended action based on signals and thresholds."""
    
    # Compute dollar impact
    if signals.trend_slope < 0:
        signals.dollars_at_risk = signals.avg_revenue * abs(signals.trend_slope) / 100
    else:
        signals.dollars_at_risk = 0
    
    if signals.trend_slope > 0:
        signals.dollars_opportunity = signals.avg_revenue * signals.trend_slope / 100
    else:
        signals.dollars_opportunity = 0
    
    # Effective threshold for low confidence
    effective_min_revenue = config.min_revenue_for_outreach
    if signals.confidence == "LOW":
        effective_min_revenue *= config.low_confidence_revenue_multiplier
    
    # Gate 1: Revenue threshold
    if signals.avg_revenue < effective_min_revenue:
        signals.recommended_action = "NO ACTION"
        signals.engagement_rationale = f"Below revenue threshold (${signals.avg_revenue:,.0f} < ${effective_min_revenue:,.0f})"
        signals.priority_score = 0
        return signals
    
    # Gate 1b: Dollar impact threshold
    max_dollar_impact = max(signals.dollars_at_risk, signals.dollars_opportunity)
    if max_dollar_impact < config.min_dollar_impact and signals.category not in ["NEW_CUSTOMER", "CHURNED"]:
        signals.recommended_action = "NO ACTION"
        signals.engagement_rationale = f"Dollar impact too low (${max_dollar_impact:,.0f}/mo). Not worth actioning."
        signals.priority_score = 0
        return signals
    
    # Special categories
    if signals.category == "CHURNED":
        signals.recommended_action = "MONITOR"
        signals.engagement_rationale = "Customer churned. Review for win-back campaign eligibility."
        signals.priority_score = 10
        return signals
    
    if signals.category == "NEW_CUSTOMER":
        signals.recommended_action = "WELCOME CALL"
        signals.engagement_rationale = f"New customer ramping up. Avg: ${signals.avg_revenue:,.0f}/month. Ensure successful onboarding."
        signals.priority_score = compute_priority_score(signals, config, base=50)
        return signals
    
    # Dollar impact overrides
    if signals.dollars_at_risk >= config.dollar_at_risk_override:
        urgency = "Urgent" if signals.dollars_at_risk >= config.dollar_at_risk_override * 2 else "High"
        signals.recommended_action = f"CHECK-IN ({urgency})"
        signals.engagement_rationale = build_risk_rationale(signals)
        signals.priority_score = compute_priority_score(signals, config, base=70 if urgency == "Urgent" else 60)
        return signals
    
    if signals.dollars_opportunity >= config.dollar_opportunity_override:
        signals.recommended_action = "EXPANSION OUTREACH"
        signals.engagement_rationale = build_expansion_rationale(signals)
        signals.priority_score = compute_priority_score(signals, config, base=55)
        return signals
    
    # Category-based actions
    if signals.category == "CHURN_RISK":
        if signals.confidence == "HIGH":
            signals.recommended_action = "CHECK-IN (Urgent)"
            signals.priority_score = compute_priority_score(signals, config, base=80)
        else:
            signals.recommended_action = "CHECK-IN (High)"
            signals.priority_score = compute_priority_score(signals, config, base=65)
        signals.engagement_rationale = build_risk_rationale(signals)
        return signals
    
    if signals.category == "RECENT_DIP":
        if signals.last_2month_change < config.recent_drop_threshold:
            signals.recommended_action = "CHECK-IN (Medium)"
            signals.engagement_rationale = build_dip_rationale(signals)
            signals.priority_score = compute_priority_score(signals, config, base=45)
        else:
            signals.recommended_action = "MONITOR"
            signals.engagement_rationale = f"Minor dip ({signals.last_2month_change:+.1%}). Monitor for continued decline."
            signals.priority_score = compute_priority_score(signals, config, base=25)
        return signals
    
    if signals.category == "VOLATILE":
        if signals.avg_revenue >= config.volatile_min_revenue:
            if signals.confidence == "HIGH" or signals.trend_slope < -3:
                signals.recommended_action = "CHECK-IN (Medium)"
                signals.engagement_rationale = build_volatile_rationale(signals)
                signals.priority_score = compute_priority_score(signals, config, base=40)
            else:
                signals.recommended_action = "MONITOR"
                signals.engagement_rationale = f"Volatile usage pattern (CV: {signals.volatility_cv:.0%}). No immediate risk."
                signals.priority_score = compute_priority_score(signals, config, base=20)
        else:
            signals.recommended_action = "NO ACTION"
            signals.engagement_rationale = f"Volatile but below threshold (${signals.avg_revenue:,.0f})"
            signals.priority_score = 5
        return signals
    
    if signals.category == "EXPANSION_OPPORTUNITY":
        if signals.trend_slope / 100 >= config.expansion_growth_threshold:
            signals.recommended_action = "EXPANSION OUTREACH"
            signals.engagement_rationale = build_expansion_rationale(signals)
            signals.priority_score = compute_priority_score(signals, config, base=50)
        else:
            signals.recommended_action = "MONITOR"
            signals.engagement_rationale = f"Moderate growth ({signals.trend_slope:+.1f}%/month). Watch for acceleration."
            signals.priority_score = compute_priority_score(signals, config, base=25)
        return signals
    
    # STAGNANT or HEALTHY
    if signals.category in ["STAGNANT", "HEALTHY"]:
        signals.recommended_action = "NO ACTION"
        signals.engagement_rationale = "Stable account. No action required."
        signals.priority_score = 5
        return signals
    
    # Default
    signals.recommended_action = "MONITOR"
    signals.engagement_rationale = "Uncategorized - review manually."
    signals.priority_score = 15
    return signals


def compute_priority_score(signals: CustomerSignals, config: AnalysisConfig, base: int = 50) -> int:
    """Compute 0-100 priority score for sorting."""
    score = base
    
    # Revenue tier boost (0-20)
    if signals.avg_revenue >= config.strategic_threshold:
        score += 20
    elif signals.avg_revenue >= config.high_value_threshold:
        score += 12
    elif signals.avg_revenue >= config.min_revenue_for_outreach * 3:
        score += 5
    
    # Confidence boost (0-10)
    if signals.confidence == "HIGH":
        score += 10
    elif signals.confidence == "MEDIUM":
        score += 5
    
    # Trend severity boost (0-10)
    if signals.trend_slope < -10:
        score += 10
    elif signals.trend_slope < -5:
        score += 5
    
    # Recent drop severity (0-10)
    if signals.last_2month_change < -0.25:
        score += 10
    elif signals.last_2month_change < -0.15:
        score += 5
    
    return min(100, max(0, score))


def build_risk_rationale(signals: CustomerSignals) -> str:
    """Build human-readable rationale for risk check-in."""
    parts = []
    
    if signals.volatility_cv > 0.5:
        parts.append(f"High volatility (CV {signals.volatility_cv:.0%})")
    
    if signals.trend_slope < -3:
        parts.append(f"declining {signals.trend_slope:+.1f}%/month")
    
    if signals.last_2month_change < -0.1:
        parts.append(f"down {abs(signals.last_2month_change):.0%} over 2 months")
    elif signals.last_month_change < -0.1:
        parts.append(f"dropped {abs(signals.last_month_change):.0%} last month")
    
    if signals.current_vs_max < 0.8:
        parts.append(f"at {signals.current_vs_max:.0%} of historical peak")
    
    if signals.dollars_at_risk > 0:
        parts.append(f"~${signals.dollars_at_risk:,.0f}/month at risk")
    
    if not parts:
        parts.append(signals.reason)
    
    return ". ".join(parts).capitalize() + "."


def build_dip_rationale(signals: CustomerSignals) -> str:
    """Build rationale for recent dip check-in."""
    parts = []
    parts.append(f"Overall trend stable ({signals.trend_slope:+.1f}%/month)")
    parts.append(f"but recent {abs(signals.last_2month_change):.0%} decline over 2 months")
    
    if signals.dollars_at_risk > 500:
        parts.append(f"~${signals.dollars_at_risk:,.0f}/month at risk if continues")
    
    return ". ".join(parts).capitalize() + ". Soft check-in recommended."


def build_volatile_rationale(signals: CustomerSignals) -> str:
    """Build rationale for volatile account check-in."""
    parts = []
    parts.append(f"Usage swings significantly (CV {signals.volatility_cv:.0%})")
    parts.append(f"max drawdown {signals.max_drawdown:.0%}")
    
    if signals.trend_slope < 0:
        parts.append(f"with downward drift ({signals.trend_slope:+.1f}%/month)")
    
    return ". ".join(parts).capitalize() + ". Usage pattern review recommended."


def build_expansion_rationale(signals: CustomerSignals) -> str:
    """Build rationale for expansion outreach."""
    parts = []
    parts.append(f"Growing {signals.trend_slope:+.1f}%/month")
    
    if signals.last_month_change > 0.05:
        parts.append(f"accelerating (+{signals.last_month_change:.0%} last month)")
    
    if signals.current_vs_max > 1.0:
        parts.append("at all-time high")
    elif signals.current_vs_max > 0.9:
        parts.append(f"near historical max ({signals.current_vs_max:.0%})")
    
    if signals.dollars_opportunity > 0:
        parts.append(f"~${signals.dollars_opportunity:,.0f}/month growth opportunity")
    
    return ". ".join(parts).capitalize() + ". Expansion conversation opportunity."


# =============================================================================
# DATABASE INTEGRATION
# =============================================================================

def run_analysis_for_all(exclude_latest_month: bool = True,
                        progress_callback: callable = None) -> dict:
    """
    Run analysis on all customers in the database.
    
    Args:
        exclude_latest_month: Whether to exclude most recent month (usually partial)
        progress_callback: Optional callback(current, total) called after each customer/bucket
        
    Returns:
        Dict with stats about the analysis run
    """
    result = None
    for update in _run_analysis_generator(exclude_latest_month):
        if update.get('complete'):
            result = update['stats']
        elif progress_callback:
            progress_callback(update['current'], update['total'])
    return result or {'analyzed': 0, 'actionable': 0, 'skipped': 0}


def run_analysis_streaming(exclude_latest_month: bool = True):
    """
    Run analysis on all customers, yielding progress dicts for SSE streaming.
    
    Yields:
        Dicts with 'current', 'total', and 'progress' keys during analysis,
        then a final dict with 'complete' True and 'stats' keys.
    """
    for update in _run_analysis_generator(exclude_latest_month):
        if update.get('complete'):
            yield update
        else:
            pct = round(update['current'] / update['total'] * 100) if update['total'] > 0 else 0
            update['progress'] = pct
            yield update


def _run_analysis_generator(exclude_latest_month: bool = True):
    """
    Core analysis generator. Yields progress dicts after each customer/bucket,
    then a final dict with complete=True and stats.
    """
    SyncStatus.mark_started('revenue_analysis')
    config = AnalysisConfig.from_db()
    
    # Get all unique customer/bucket combinations
    customer_buckets = db.session.query(
        CustomerRevenueData.customer_name,
        CustomerRevenueData.bucket,
        CustomerRevenueData.tpid
    ).distinct().all()
    
    if not customer_buckets:
        stats = {'analyzed': 0, 'actionable': 0, 'skipped': 0}
        SyncStatus.mark_completed('revenue_analysis', success=True,
                                  items_synced=0, details='No customer data to analyze')
        yield {'complete': True, 'stats': stats}
        return
    
    # Build customer_id lookup from revenue data (set during import with fuzzy matching)
    # This is more accurate than name matching since import uses progressive word-prefix
    # and acronym matching to link CSV customer names to NoteHelper customers
    customer_id_pairs = db.session.query(
        CustomerRevenueData.customer_name,
        db.func.max(CustomerRevenueData.customer_id)
    ).filter(
        CustomerRevenueData.customer_id.isnot(None)
    ).group_by(
        CustomerRevenueData.customer_name
    ).all()
    customer_id_from_revenue = {name: cid for name, cid in customer_id_pairs}
    
    # Build Customer lookup by ID for seller/tpid info
    all_customers = Customer.query.options(
        db.joinedload(Customer.seller)
    ).all()
    customer_by_id = {c.id: c for c in all_customers}
    
    # Determine months to analyze
    all_months = db.session.query(
        CustomerRevenueData.month_date
    ).distinct().order_by(CustomerRevenueData.month_date).all()
    
    month_dates = [m[0] for m in all_months]
    
    if exclude_latest_month and len(month_dates) > 1:
        month_dates = month_dates[:-1]  # Exclude most recent
    
    stats = {'analyzed': 0, 'actionable': 0, 'skipped': 0}
    total_buckets = len(customer_buckets)
    
    for idx, cb in enumerate(customer_buckets):
        customer_name, bucket, tpid = cb
        
        # Get customer_id from revenue data (set during import with fuzzy matching)
        customer_id = customer_id_from_revenue.get(customer_name)
        if customer_id:
            existing_customer = customer_by_id.get(customer_id)
            seller_name = existing_customer.seller.name if existing_customer and existing_customer.seller else None
            # Use TPID from our database if not in revenue data
            if not tpid and existing_customer and existing_customer.tpid:
                tpid = existing_customer.tpid
        else:
            seller_name = None
        
        # Get revenue data for this customer/bucket
        revenue_data = CustomerRevenueData.query.filter(
            CustomerRevenueData.customer_name == customer_name,
            CustomerRevenueData.bucket == bucket,
            CustomerRevenueData.month_date.in_(month_dates)
        ).order_by(CustomerRevenueData.month_date).all()
        
        if len(revenue_data) < 3:
            stats['skipped'] += 1
            yield {'current': idx + 1, 'total': total_buckets}
            continue
        
        revenues = [rd.revenue for rd in revenue_data]
        month_names = [rd.fiscal_month for rd in revenue_data]
        
        # Compute signals
        signals = compute_signals(
            customer_name=customer_name,
            bucket=bucket,
            revenues=revenues,
            month_names=month_names,
            tpid=tpid,
            seller_name=seller_name,
            customer_id=customer_id
        )
        
        if not signals:
            stats['skipped'] += 1
            yield {'current': idx + 1, 'total': total_buckets}
            continue
        
        # Determine action
        signals = determine_action(signals, config)
        
        # Save to database (upsert)
        analysis = RevenueAnalysis.query.filter_by(
            customer_name=customer_name,
            bucket=bucket
        ).first()
        
        is_new = analysis is None
        if is_new:
            analysis = RevenueAnalysis(
                customer_name=customer_name,
                bucket=bucket
            )
        else:
            # Track if category changed
            if analysis.category != signals.category:
                analysis.previous_category = analysis.category
                analysis.previous_priority_score = analysis.priority_score
                analysis.status_changed_at = datetime.now(timezone.utc)
        
        # Update fields
        analysis.customer_id = customer_id
        analysis.tpid = tpid
        analysis.seller_name = seller_name
        analysis.analyzed_at = datetime.now(timezone.utc)
        analysis.months_analyzed = len(revenues)
        analysis.avg_revenue = signals.avg_revenue
        analysis.latest_revenue = revenues[-1]
        analysis.category = signals.category
        analysis.recommended_action = signals.recommended_action
        analysis.confidence = signals.confidence
        analysis.priority_score = signals.priority_score
        analysis.dollars_at_risk = signals.dollars_at_risk
        analysis.dollars_opportunity = signals.dollars_opportunity
        analysis.trend_slope = signals.trend_slope
        analysis.last_month_change = signals.last_month_change
        analysis.last_2month_change = signals.last_2month_change
        analysis.volatility_cv = signals.volatility_cv
        analysis.max_drawdown = signals.max_drawdown
        analysis.current_vs_max = signals.current_vs_max
        analysis.current_vs_avg = signals.current_vs_avg
        analysis.engagement_rationale = signals.engagement_rationale
        
        if is_new:
            db.session.add(analysis)
        
        stats['analyzed'] += 1
        if signals.recommended_action not in ["NO ACTION", "MONITOR"]:
            stats['actionable'] += 1
        
        yield {'current': idx + 1, 'total': total_buckets}
    
    db.session.commit()

    SyncStatus.mark_completed(
        'revenue_analysis', success=True,
        items_synced=stats['analyzed'],
        details=f"{stats['analyzed']} analyzed, {stats['actionable']} actionable, {stats['skipped']} skipped"
    )
    
    yield {'complete': True, 'stats': stats}


def get_actionable_analyses(
    min_priority: int = 0,
    categories: Optional[list[str]] = None,
    seller_name: Optional[str] = None,
    limit: int = 100
) -> list[RevenueAnalysis]:
    """
    Get analyses that need action, sorted by priority.
    
    Args:
        min_priority: Minimum priority score
        categories: List of categories to include (None = all actionable)
        seller_name: Filter by seller
        limit: Max results
        
    Returns:
        List of RevenueAnalysis records
    """
    query = RevenueAnalysis.query.filter(
        RevenueAnalysis.recommended_action.notin_(["NO ACTION"]),
        RevenueAnalysis.priority_score >= min_priority
    )
    
    if categories:
        query = query.filter(RevenueAnalysis.category.in_(categories))
    
    if seller_name:
        query = query.filter(RevenueAnalysis.seller_name == seller_name)
    
    return query.order_by(RevenueAnalysis.priority_score.desc()).limit(limit).all()


def get_seller_alerts(seller_name: str) -> list[RevenueAnalysis]:
    """Get all actionable analyses for a specific seller."""
    return RevenueAnalysis.query.filter(
        RevenueAnalysis.seller_name == seller_name,
        RevenueAnalysis.recommended_action.notin_(["NO ACTION", "MONITOR"])
    ).order_by(RevenueAnalysis.priority_score.desc()).all()
