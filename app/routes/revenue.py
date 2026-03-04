"""
Revenue routes for NoteHelper.
Handles revenue data import, analysis, and attention dashboard.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, Response, stream_with_context
from werkzeug.utils import secure_filename
import csv
import json
import time
import tempfile
from io import StringIO

from app.models import (
    db, RevenueImport, CustomerRevenueData, ProductRevenueData, RevenueAnalysis, 
    RevenueConfig, RevenueEngagement, Customer, Seller, SyncStatus
)
from app.services.revenue_import import (
    import_revenue_csv, get_import_history, get_months_in_database,
    get_customer_revenue_history, get_product_revenue_history,
    get_products_for_bucket, get_all_products, get_customers_using_product,
    get_seller_products, get_seller_customers_using_product,
    consolidate_products_list, consolidate_product_name,
    import_revenue_csv_streaming,
    RevenueImportError
)
from app.services.revenue_analysis import (
    run_analysis_for_all, run_analysis_streaming, get_actionable_analyses,
    get_seller_alerts, AnalysisConfig
)

# Create blueprint
revenue_bp = Blueprint('revenue', __name__)


@revenue_bp.route('/revenue')
def revenue_dashboard():
    """Main revenue attention dashboard."""
    # Get actionable analyses
    analyses = get_actionable_analyses(min_priority=20, limit=50)
    
    # Group by category for summary
    category_counts = {}
    for a in analyses:
        cat = a.category
        if cat not in category_counts:
            category_counts[cat] = {'count': 0, 'total_at_risk': 0, 'total_opportunity': 0}
        category_counts[cat]['count'] += 1
        category_counts[cat]['total_at_risk'] += a.dollars_at_risk or 0
        category_counts[cat]['total_opportunity'] += a.dollars_opportunity or 0
    
    # Get unique sellers with alerts
    seller_names = db.session.query(RevenueAnalysis.seller_name).filter(
        RevenueAnalysis.seller_name.isnot(None),
        RevenueAnalysis.recommended_action.notin_(["NO ACTION", "MONITOR"])
    ).distinct().all()
    sellers_with_alerts = [s[0] for s in seller_names if s[0]]
    
    # Get import stats
    latest_import = RevenueImport.query.order_by(RevenueImport.imported_at.desc()).first()
    months_data = get_months_in_database()
    
    # Get sync status for warning banners
    import_status = SyncStatus.get_status('revenue_import')
    analysis_status = SyncStatus.get_status('revenue_analysis')
    
    return render_template(
        'revenue_dashboard.html',
        analyses=analyses,
        category_counts=category_counts,
        sellers_with_alerts=sellers_with_alerts,
        latest_import=latest_import,
        months_data=months_data,
        import_status=import_status,
        analysis_status=analysis_status,
    )


@revenue_bp.route('/revenue/import', methods=['GET', 'POST'])
def revenue_import():
    """Import revenue CSV data (form display only, POST redirects to streaming)."""
    if request.method == 'POST':
        # Redirect to dashboard - actual import handled by streaming endpoint
        flash('Please use the import form', 'info')
        return redirect(request.url)
    
    # GET - show import form
    import_history = get_import_history(limit=10)
    months_data = get_months_in_database()
    
    return render_template(
        'revenue_import.html',
        import_history=import_history,
        months_data=months_data
    )


@revenue_bp.route('/api/revenue/import', methods=['POST'])
def revenue_import_stream():
    """Import revenue CSV with streaming progress updates."""
    from app.models import Customer
    if Customer.query.first() is None:
        return jsonify({'error': 'Import accounts first'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'Only CSV files are supported'}), 400
    
    filename = secure_filename(file.filename)
    content = file.read()
    run_analysis = request.form.get('run_analysis', 'on') not in ('off', 'no', '0', '')
    user_id = g.user.id
    
    def generate():
        """Generator for streaming progress updates."""
        try:
            import_start_time = time.time()
            # Stream import progress
            import_result = None
            for progress in import_revenue_csv_streaming(content, filename, user_id):
                if progress.get('complete'):
                    import_result = progress.get('result')
                else:
                    yield "data: " + json.dumps(progress) + "\n\n"
            
            if not import_result:
                yield "data: " + json.dumps({"error": "Import failed - no result"}) + "\n\n"
                return
            
            # Run analysis if requested
            if run_analysis:
                yield "data: " + json.dumps({"message": "Analyzing revenue trends...", "analysis_started": True}) + "\n\n"
                
                analysis_stats = None
                for update in run_analysis_streaming(user_id=user_id):
                    if update.get('complete'):
                        analysis_stats = update['stats']
                    else:
                        yield "data: " + json.dumps({
                            "message": f"Analyzing customer {update['current']} of {update['total']}...",
                            "progress": update['progress']
                        }) + "\n\n"
                
                if analysis_stats:
                    yield "data: " + json.dumps({
                        "message": f"Analysis complete: {analysis_stats['analyzed']} customers, {analysis_stats['actionable']} need attention"
                    }) + "\n\n"
            
            # Send final result
            yield "data: " + json.dumps({
                "result": {
                    "records_created": import_result.records_created,
                    "records_updated": import_result.records_updated,
                    "new_months": import_result.new_months_added,
                    "duration": round(time.time() - import_start_time, 1),
                }
            }) + "\n\n"
            
        except RevenueImportError as e:
            SyncStatus.mark_completed('revenue_import', success=False, details=str(e))
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
        except Exception as e:
            SyncStatus.mark_completed('revenue_import', success=False, details=str(e))
            yield "data: " + json.dumps({"error": f"Unexpected error: {str(e)}"}) + "\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@revenue_bp.route('/revenue/analyze', methods=['POST'])
def revenue_analyze():
    """Re-run analysis on all revenue data."""
    from app.models import Customer
    if Customer.query.first() is None:
        flash('Import accounts first before running analysis.', 'warning')
        return redirect(url_for('revenue.revenue_dashboard'))

    try:
        stats = run_analysis_for_all(user_id=g.user.id)
        flash(
            f'Analysis complete: {stats["analyzed"]} customers analyzed, '
            f'{stats["actionable"]} need attention, '
            f'{stats["skipped"]} skipped (insufficient data).',
            'success'
        )
    except Exception as e:
        flash(f'Analysis error: {str(e)}', 'error')
    
    return redirect(url_for('revenue.revenue_dashboard'))


@revenue_bp.route('/revenue/seller/<seller_name>')
def revenue_seller_view(seller_name: str):
    """View revenue analysis for a specific seller."""
    # Get alerts for this seller
    alerts = get_seller_alerts(seller_name)
    
    # Calculate totals
    total_at_risk = sum(a.dollars_at_risk or 0 for a in alerts)
    total_opportunity = sum(a.dollars_opportunity or 0 for a in alerts)
    
    # Try to match to a NoteHelper Seller
    seller = Seller.query.filter(
        db.func.lower(Seller.name) == seller_name.lower()
    ).first()
    
    return render_template(
        'revenue_seller_alerts.html',
        seller_name=seller_name,
        seller=seller,
        alerts=alerts,
        total_at_risk=total_at_risk,
        total_opportunity=total_opportunity
    )


@revenue_bp.route('/revenue/seller/<seller_name>/export')
def revenue_seller_export(seller_name: str):
    """Export seller's alerts as CSV for sending via Teams with product details."""
    alerts = get_seller_alerts(seller_name)
    
    # Get all months in database for product columns
    all_months_data = get_months_in_database()
    # Sort chronologically and take last 7
    recent_months = [m['fiscal_month'] for m in all_months_data[-7:]] if all_months_data else []
    
    output = StringIO()
    writer = csv.writer(output)
    
    # Header - includes month columns for products
    header = [
        'Customer', 'TPID', 'Bucket', 'Product', 'Category',
        'Recommended Action', 'Rationale', '$ At Risk/Month', '$ Opportunity/Month',
        'Priority Score', 'Trend %/Month'
    ]
    header.extend(recent_months)
    header.append('Total')
    writer.writerow(header)
    
    # Data rows with product breakdown
    for a in alerts:
        # Write the bucket summary row
        bucket_row = [
            a.customer_name,
            a.tpid or '',
            a.bucket,
            '** BUCKET TOTAL **',
            a.category,
            a.recommended_action,
            a.engagement_rationale,
            f'${a.dollars_at_risk:,.0f}' if a.dollars_at_risk else '',
            f'${a.dollars_opportunity:,.0f}' if a.dollars_opportunity else '',
            a.priority_score,
            f'{a.trend_slope:+.1f}%'
        ]
        # Get bucket totals by month
        bucket_history = get_customer_revenue_history(a.customer_name, a.bucket)
        bucket_month_revenues = {rd.fiscal_month: rd.revenue for rd in bucket_history}
        bucket_total = sum(rd.revenue for rd in bucket_history)
        for month in recent_months:
            rev = bucket_month_revenues.get(month)
            bucket_row.append(f'${rev:,.0f}' if rev else '')
        bucket_row.append(f'${bucket_total:,.0f}')
        writer.writerow(bucket_row)
        
        # Get products for this bucket and write product rows
        products = get_products_for_bucket(a.customer_name, a.bucket)
        for p in products:
            product_history = get_product_revenue_history(a.customer_name, a.bucket, p['product'])
            month_revenues = {rd.fiscal_month: rd.revenue for rd in product_history}
            product_total = sum(rd.revenue for rd in product_history)
            
            product_row = [
                '',  # Customer name only on first row
                '',  # TPID
                '',  # Bucket
                p['product'],
                '',  # Category
                '',  # Recommended Action
                '',  # Rationale
                '',  # $ At Risk
                '',  # $ Opportunity
                '',  # Priority Score
                ''   # Trend
            ]
            for month in recent_months:
                rev = month_revenues.get(month)
                product_row.append(f'${rev:,.0f}' if rev else '')
            product_row.append(f'${product_total:,.0f}')
            writer.writerow(product_row)
        
        # Add blank row between customers for readability
        writer.writerow([])
    
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename={seller_name}_revenue_analysis.csv'
        }
    )


@revenue_bp.route('/revenue/seller/<seller_name>/products')
def revenue_seller_products(seller_name: str):
    """View all products used by a seller's customers."""
    products = get_seller_products(seller_name)
    
    # Consolidate products (e.g., roll up Azure Synapse Analytics*)
    products = consolidate_products_list(products)
    
    # Handle sorting
    sort = request.args.get('sort', 'revenue')
    if sort == 'customers':
        products = sorted(products, key=lambda x: x['customer_count'], reverse=True)
    else:  # default to revenue
        sort = 'revenue'
        products = sorted(products, key=lambda x: x['total_revenue'], reverse=True)
    
    # Try to match to a NoteHelper Seller
    seller = Seller.query.filter(
        db.func.lower(Seller.name) == seller_name.lower()
    ).first()
    
    return render_template(
        'revenue_seller_products.html',
        seller_name=seller_name,
        seller=seller,
        products=products,
        sort=sort
    )


@revenue_bp.route('/revenue/seller/<seller_name>/product/<path:product>')
def revenue_seller_product_view(seller_name: str, product: str):
    """View seller's customers using a specific product with revenue grid."""
    from app.services.revenue_import import PRODUCT_CONSOLIDATION_PREFIXES
    
    # Check if this is a consolidated product (e.g., "Azure Synapse Analytics")
    is_consolidated = product in PRODUCT_CONSOLIDATION_PREFIXES
    
    if is_consolidated:
        # Get all sub-products for this consolidated product
        # Query all products that start with this prefix
        all_seller_products = get_seller_products(seller_name)
        matching_products = [p['product'] for p in all_seller_products if p['product'].startswith(product)]
    else:
        matching_products = [product]
    
    # Aggregate customers across all matching products
    customers_dict = {}
    for prod in matching_products:
        prod_customers = get_seller_customers_using_product(seller_name, prod)
        for c in prod_customers:
            key = (c['customer_name'], c['bucket'])
            if key not in customers_dict:
                customers_dict[key] = {
                    'customer_name': c['customer_name'],
                    'bucket': c['bucket'],
                    'customer_id': c.get('customer_id'),
                    'total_revenue': 0,
                    'latest_month': c.get('latest_month')
                }
            customers_dict[key]['total_revenue'] += c['total_revenue']
    
    customers = list(customers_dict.values())
    customers.sort(key=lambda x: x['total_revenue'], reverse=True)
    
    # Get historical revenue for each customer (aggregated across matching products)
    customer_history = {}
    all_months = {}
    
    for c in customers:
        aggregated_history = {}
        for prod in matching_products:
            history = ProductRevenueData.query.filter_by(
                customer_name=c['customer_name'],
                bucket=c['bucket'],
                product=prod
            ).order_by(ProductRevenueData.month_date).all()
            for rd in history:
                if rd.fiscal_month not in aggregated_history:
                    aggregated_history[rd.fiscal_month] = {
                        'fiscal_month': rd.fiscal_month,
                        'month_date': rd.month_date,
                        'revenue': 0
                    }
                aggregated_history[rd.fiscal_month]['revenue'] += rd.revenue
                all_months[rd.fiscal_month] = rd.month_date
        
        if aggregated_history:
            customer_history[c['customer_name']] = list(aggregated_history.values())
    
    # Get 7 most recent months sorted chronologically
    sorted_months = sorted(all_months.items(), key=lambda x: x[1])
    recent_months = [m[0] for m in sorted_months[-7:]]
    
    # Build customer summary with monthly revenues
    customer_summary = []
    for c in customers:
        history = customer_history.get(c['customer_name'], [])
        month_revenues = {h['fiscal_month']: h['revenue'] for h in history}
        customer_summary.append({
            'customer_name': c['customer_name'],
            'bucket': c['bucket'],
            'total_revenue': c['total_revenue'],
            'month_revenues': month_revenues
        })
    
    # Try to match to a NoteHelper Seller
    seller = Seller.query.filter(
        db.func.lower(Seller.name) == seller_name.lower()
    ).first()
    
    return render_template(
        'revenue_seller_product_view.html',
        seller_name=seller_name,
        seller=seller,
        product=product,
        customers=customers,
        customer_summary=customer_summary,
        recent_months=recent_months,
        is_consolidated=is_consolidated,
        sub_products=matching_products if is_consolidated else None
    )


@revenue_bp.route('/revenue/customer/<int:customer_id>')
def revenue_customer_view(customer_id: int):
    """View revenue history and analysis for a specific customer."""
    # Get the NoteHelper customer
    customer = db.session.get(Customer, customer_id)
    if not customer:
        flash('Customer not found.', 'danger')
        return redirect(url_for('revenue.revenue_dashboard'))
    
    # Query revenue data by customer_id (set during import with fuzzy matching)
    # Display the NoteHelper customer name in the UI
    customer_name = customer.name
    
    # Get all analyses for this customer (all buckets)
    analyses = RevenueAnalysis.query.filter_by(customer_id=customer_id).all()
    
    # Get revenue history by bucket
    buckets = ['Core DBs', 'Analytics', 'Modern DBs']
    revenue_by_bucket = {}
    products_by_bucket = {}
    bucket_product_data = {}  # Full product data with monthly revenues
    
    for bucket in buckets:
        history = get_customer_revenue_history(bucket=bucket, customer_id=customer_id)
        if history:
            revenue_by_bucket[bucket] = history
            # Get products for this bucket
            products = get_products_for_bucket(bucket=bucket, customer_id=customer_id)
            products_by_bucket[bucket] = products
            
            # Get product history for grid display
            product_history = {}
            for p in products:
                p_history = get_product_revenue_history(
                    bucket=bucket, product=p['product'], customer_id=customer_id
                )
                if p_history:
                    product_history[p['product']] = p_history
            
            # Get the 7 most recent months for this bucket
            all_months = {}
            for ph in product_history.values():
                for rd in ph:
                    all_months[rd.fiscal_month] = rd.month_date
            # Also include bucket totals months
            for rd in history:
                all_months[rd.fiscal_month] = rd.month_date
            sorted_months = sorted(all_months.items(), key=lambda x: x[1])
            recent_months = [m[0] for m in sorted_months[-7:]]
            
            # Build product summary with monthly revenues
            product_summary = []
            for p in products:
                p_hist = product_history.get(p['product'], [])
                month_revenues = {rd.fiscal_month: rd.revenue for rd in p_hist}
                product_summary.append({
                    'product': p['product'],
                    'total_revenue': p['total_revenue'],
                    'month_revenues': month_revenues
                })
            
            # Build bucket total monthly revenues
            bucket_month_revenues = {rd.fiscal_month: rd.revenue for rd in history}
            
            bucket_product_data[bucket] = {
                'recent_months': recent_months,
                'product_summary': product_summary,
                'bucket_month_revenues': bucket_month_revenues,
                'bucket_total': sum(rd.revenue for rd in history)
            }
    
    return render_template(
        'revenue_customer_view.html',
        customer_name=customer_name,
        customer=customer,
        analyses=analyses,
        revenue_by_bucket=revenue_by_bucket,
        products_by_bucket=products_by_bucket,
        bucket_product_data=bucket_product_data
    )


@revenue_bp.route('/revenue/customer/<int:customer_id>/bucket/<bucket>')
def revenue_bucket_products(customer_id: int, bucket: str):
    """View product-level revenue breakdown for a customer/bucket."""
    # Get the NoteHelper customer
    customer = db.session.get(Customer, customer_id)
    if not customer:
        flash('Customer not found.', 'danger')
        return redirect(url_for('revenue.revenue_dashboard'))
    
    customer_name = customer.name
    # Get products with totals (query by customer_id for fuzzy-matched customers)
    products = get_products_for_bucket(bucket=bucket, customer_id=customer_id)
    
    # Get product history for drill-down
    product_history = {}
    for p in products:
        history = get_product_revenue_history(
            bucket=bucket, product=p['product'], customer_id=customer_id
        )
        if history:
            product_history[p['product']] = history
    
    # Get the 7 most recent months across all products for the summary table
    # Use (month_date, fiscal_month) tuples to sort chronologically
    all_months = {}
    for history in product_history.values():
        for rd in history:
            all_months[rd.fiscal_month] = rd.month_date
    # Sort by actual date, then take most recent 7
    sorted_months = sorted(all_months.items(), key=lambda x: x[1])
    recent_months = [m[0] for m in sorted_months[-7:]]
    
    # Build summary data for each product: monthly revenue for recent months
    product_summary = []
    for p in products:
        history = product_history.get(p['product'], [])
        month_revenues = {rd.fiscal_month: rd.revenue for rd in history}
        product_summary.append({
            'product': p['product'],
            'total_revenue': p['total_revenue'],
            'month_revenues': month_revenues
        })
    
    # Get the bucket-level analysis if it exists
    analysis = RevenueAnalysis.query.filter_by(
        customer_id=customer_id,
        bucket=bucket
    ).first()
    
    return render_template(
        'revenue_bucket_products.html',
        customer_name=customer_name,
        customer=customer,
        bucket=bucket,
        products=products,
        product_history=product_history,
        product_summary=product_summary,
        recent_months=recent_months,
        analysis=analysis
    )


@revenue_bp.route('/revenue/products')
def revenue_products_list():
    """List all products with usage statistics."""
    products = get_all_products()
    # Consolidate products (e.g., roll up Azure Synapse Analytics*)
    products = consolidate_products_list(products)
    # Sort by total revenue
    products = sorted(products, key=lambda x: x['total_revenue'], reverse=True)
    return render_template('revenue_products_list.html', products=products)


@revenue_bp.route('/revenue/product/<path:product>')
def revenue_product_view(product: str):
    """View all customers using a specific product."""
    from app.services.revenue_import import PRODUCT_CONSOLIDATION_PREFIXES
    
    # Check if this is a consolidated product (e.g., "Azure Synapse Analytics")
    is_consolidated = product in PRODUCT_CONSOLIDATION_PREFIXES
    
    if is_consolidated:
        # Get all sub-products for this consolidated product
        all_products = get_all_products()
        matching_products = [p['product'] for p in all_products if p['product'].startswith(product)]
    else:
        matching_products = [product]
    
    # Aggregate customers across all matching products
    customers_dict = {}
    for prod in matching_products:
        prod_customers = get_customers_using_product(prod)
        for c in prod_customers:
            key = (c['customer_name'], c['bucket'])
            if key not in customers_dict:
                customers_dict[key] = {
                    'customer_name': c['customer_name'],
                    'bucket': c['bucket'],
                    'customer_id': c.get('customer_id'),
                    'total_revenue': 0,
                    'latest_month': c.get('latest_month')
                }
            customers_dict[key]['total_revenue'] += c['total_revenue']
    
    customers = list(customers_dict.values())
    customers.sort(key=lambda x: x['total_revenue'], reverse=True)
    
    # Get historical revenue for each customer (aggregated across matching products)
    customer_history = {}
    for c in customers:
        aggregated_history = {}
        for prod in matching_products:
            history = ProductRevenueData.query.filter_by(
                customer_name=c['customer_name'],
                bucket=c['bucket'],
                product=prod
            ).order_by(ProductRevenueData.month_date).all()
            for rd in history:
                if rd.fiscal_month not in aggregated_history:
                    aggregated_history[rd.fiscal_month] = {
                        'fiscal_month': rd.fiscal_month,
                        'month_date': rd.month_date,
                        'revenue': 0
                    }
                aggregated_history[rd.fiscal_month]['revenue'] += rd.revenue
        
        if aggregated_history:
            customer_history[c['customer_name']] = list(aggregated_history.values())
    
    return render_template(
        'revenue_product_view.html',
        product=product,
        customers=customers,
        customer_history=customer_history,
        is_consolidated=is_consolidated,
        sub_products=matching_products if is_consolidated else None
    )


@revenue_bp.route('/revenue/config', methods=['GET', 'POST'])
def revenue_config():
    """Configure revenue analysis thresholds."""
    config = RevenueConfig.query.filter_by(user_id=g.user.id).first()
    
    if request.method == 'POST':
        if not config:
            config = RevenueConfig(user_id=g.user.id)
            db.session.add(config)
        
        # Update values from form
        config.min_revenue_for_outreach = int(request.form.get('min_revenue_for_outreach', 3000))
        config.min_dollar_impact = int(request.form.get('min_dollar_impact', 1000))
        config.dollar_at_risk_override = int(request.form.get('dollar_at_risk_override', 2000))
        config.dollar_opportunity_override = int(request.form.get('dollar_opportunity_override', 1500))
        config.high_value_threshold = int(request.form.get('high_value_threshold', 25000))
        config.strategic_threshold = int(request.form.get('strategic_threshold', 50000))
        config.volatile_min_revenue = int(request.form.get('volatile_min_revenue', 5000))
        config.recent_drop_threshold = float(request.form.get('recent_drop_threshold', -0.15))
        config.expansion_growth_threshold = float(request.form.get('expansion_growth_threshold', 0.08))
        
        db.session.commit()
        flash('Configuration saved', 'success')
        return redirect(url_for('revenue.revenue_dashboard'))
    
    # Use defaults if no config exists
    defaults = AnalysisConfig()
    
    return render_template(
        'revenue_config.html',
        config=config,
        defaults=defaults
    )


# API endpoints for AJAX operations

@revenue_bp.route('/api/revenue/analysis/<int:analysis_id>')
def api_get_analysis(analysis_id: int):
    """Get analysis details as JSON."""
    analysis = RevenueAnalysis.query.get_or_404(analysis_id)
    
    return jsonify({
        'id': analysis.id,
        'customer_name': analysis.customer_name,
        'bucket': analysis.bucket,
        'category': analysis.category,
        'recommended_action': analysis.recommended_action,
        'engagement_rationale': analysis.engagement_rationale,
        'priority_score': analysis.priority_score,
        'dollars_at_risk': analysis.dollars_at_risk,
        'dollars_opportunity': analysis.dollars_opportunity,
        'avg_revenue': analysis.avg_revenue,
        'trend_slope': analysis.trend_slope,
        'confidence': analysis.confidence,
        'seller_name': analysis.seller_name,
        'tpid': analysis.tpid
    })


@revenue_bp.route('/api/revenue/stats')
def api_revenue_stats():
    """Get overall revenue analysis stats."""
    total_analyses = RevenueAnalysis.query.count()
    actionable = RevenueAnalysis.query.filter(
        RevenueAnalysis.recommended_action.notin_(["NO ACTION", "MONITOR"])
    ).count()
    
    # Category breakdown
    categories = db.session.query(
        RevenueAnalysis.category,
        db.func.count(RevenueAnalysis.id)
    ).group_by(RevenueAnalysis.category).all()
    
    return jsonify({
        'total_analyses': total_analyses,
        'actionable': actionable,
        'categories': {c: count for c, count in categories}
    })


# ============ Engagement Tracking Routes ============

@revenue_bp.route('/revenue/engagement/<int:analysis_id>', methods=['GET', 'POST'])
def record_engagement(analysis_id: int):
    """Record engagement for a revenue analysis."""
    analysis = RevenueAnalysis.query.get_or_404(analysis_id)
    
    if request.method == 'POST':
        from datetime import datetime, timezone
        
        status = request.form.get('status', 'pending')
        seller_response = request.form.get('seller_response', '').strip()
        resolution_notes = request.form.get('resolution_notes', '').strip()
        
        engagement = RevenueEngagement(
            analysis_id=analysis_id,
            assigned_to_seller=analysis.seller_name,
            category_when_sent=analysis.category,
            action_when_sent=analysis.recommended_action,
            rationale_when_sent=analysis.engagement_rationale,
            status=status
        )
        
        # Set response fields if provided
        if seller_response:
            engagement.seller_response = seller_response
            engagement.response_date = datetime.now(timezone.utc)
        
        # Set resolution fields if resolved
        if status == 'resolved' and resolution_notes:
            engagement.resolution_notes = resolution_notes
            engagement.resolved_at = datetime.now(timezone.utc)
        
        db.session.add(engagement)
        db.session.commit()
        
        flash(f'Engagement recorded for {analysis.customer_name}', 'success')
        
        # Redirect back to referrer or dashboard
        next_url = request.form.get('next', url_for('revenue.revenue_dashboard'))
        return redirect(next_url)
    
    # GET - show engagement form
    existing_engagements = RevenueEngagement.query.filter_by(
        analysis_id=analysis_id
    ).order_by(RevenueEngagement.created_at.desc()).all()
    
    return render_template('revenue_engagement.html', 
                          analysis=analysis,
                          engagements=existing_engagements)


@revenue_bp.route('/api/revenue/engagement/<int:analysis_id>', methods=['POST'])
def api_record_engagement(analysis_id: int):
    """API endpoint to record engagement (for modals)."""
    from datetime import datetime, timezone
    
    analysis = RevenueAnalysis.query.get_or_404(analysis_id)
    
    data = request.get_json() or {}
    status = data.get('status', 'pending')
    seller_response = data.get('seller_response', '')
    resolution_notes = data.get('resolution_notes', '')
    
    engagement = RevenueEngagement(
        analysis_id=analysis_id,
        assigned_to_seller=analysis.seller_name,
        category_when_sent=analysis.category,
        action_when_sent=analysis.recommended_action,
        rationale_when_sent=analysis.engagement_rationale,
        status=status
    )
    
    if seller_response:
        engagement.seller_response = seller_response
        engagement.response_date = datetime.now(timezone.utc)
    
    if status == 'resolved' and resolution_notes:
        engagement.resolution_notes = resolution_notes
        engagement.resolved_at = datetime.now(timezone.utc)
    
    db.session.add(engagement)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'engagement_id': engagement.id,
        'message': f'Engagement recorded for {analysis.customer_name}'
    })


@revenue_bp.route('/revenue/engagements')
def engagement_history():
    """View all engagement history."""
    engagements = RevenueEngagement.query.options(
        db.joinedload(RevenueEngagement.analysis)
    ).order_by(RevenueEngagement.created_at.desc()).limit(100).all()
    
    return render_template('revenue_engagement_history.html', engagements=engagements)


@revenue_bp.route('/api/revenue/engagement/<int:engagement_id>', methods=['DELETE'])
def api_delete_engagement(engagement_id: int):
    """Delete an engagement record."""
    engagement = RevenueEngagement.query.get_or_404(engagement_id)
    
    db.session.delete(engagement)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Engagement deleted'})


# =============================================================================
# BESPOKE REPORTS
# =============================================================================

@revenue_bp.route('/revenue/reports')
def reports_list():
    """List of available bespoke reports."""
    reports = [
        {
            'id': 'new-synapse-users',
            'name': 'New Azure Synapse Analytics Users',
            'description': 'Customers who have started using Azure Synapse Analytics in the last 6 months, grouped by seller.',
            'icon': 'bi-database-gear',
            'url': url_for('revenue.report_new_synapse_users')
        },
    ]
    
    return render_template('revenue_reports_list.html', reports=reports)


@revenue_bp.route('/revenue/reports/new-synapse-users')
def report_new_synapse_users():
    """Report: Customers who recently started using Azure Synapse Analytics."""
    from app.services.revenue_import import get_new_product_users, get_months_in_database
    
    # Get new users for Azure Synapse Analytics
    new_users = get_new_product_users('Azure Synapse Analytics', months_lookback=6)
    
    # Group by seller
    sellers = {}
    for user in new_users:
        seller = user['seller_name'] or '(No Seller Assigned)'
        if seller not in sellers:
            sellers[seller] = []
        sellers[seller].append(user)
    
    # Get months for context
    months = get_months_in_database()
    lookback_months = months[-6:] if len(months) >= 6 else months
    
    return render_template(
        'revenue_report_new_synapse_users.html',
        sellers=sellers,
        new_users=new_users,
        total_count=len(new_users),
        lookback_months=lookback_months,
        product_name='Azure Synapse Analytics'
    )
