"""
Customer routes for NoteHelper.
Handles customer listing, creation, viewing, editing, and TPID workflow.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
from sqlalchemy import func, or_

from app.models import db, Customer, CustomerCSAM, Seller, Territory, Note, UserPreference
from app.services.backup import backup_customer as _backup_customer

# Create blueprint
customers_bp = Blueprint('customers', __name__)


@customers_bp.route('/customers')
def customers_list():
    """List all customers - alphabetical, grouped by seller, or sorted by call count based on preference."""
    pref = UserPreference.query.first()
    
    # Check preference for showing customers without calls (default: True = show all)
    show_customers_without_calls = pref.show_customers_without_calls if pref else True
    
    # Determine sort method - check new field first, fall back to old grouped field for backwards compatibility
    sort_by = pref.customer_sort_by if pref else 'alphabetical'
    if sort_by == 'grouped' or (pref and pref.customer_view_grouped and sort_by == 'alphabetical'):
        sort_by = 'grouped'
    
    if sort_by == 'grouped':
        # Grouped view - get all sellers with their customers
        sellers = Seller.query.options(
            db.joinedload(Seller.customers).joinedload(Customer.notes),
            db.joinedload(Seller.customers).joinedload(Customer.territory),
            db.joinedload(Seller.territories)
        ).order_by(Seller.name).all()
        
        # Build grouped data structure
        grouped_customers = []
        for seller in sellers:
            customers = sorted(seller.customers, key=lambda c: c.name)
            
            # Filter out customers without calls if preference is False
            if not show_customers_without_calls:
                customers = [c for c in customers if len(c.notes) > 0]
            
            if customers:
                grouped_customers.append({
                    'seller': seller,
                    'customers': customers
                })
        
        # Get customers without a seller
        customers_without_seller_query = Customer.query.options(
            db.joinedload(Customer.notes),
            db.joinedload(Customer.territory)
        ).filter_by(seller_id=None).order_by(Customer.name)
        
        # Filter out customers without calls if preference is False
        if not show_customers_without_calls:
            customers_without_seller = [c for c in customers_without_seller_query.all() if len(c.notes) > 0]
        else:
            customers_without_seller = customers_without_seller_query.all()
        
        return render_template('customers_list.html', 
                             grouped_customers=grouped_customers,
                             customers_without_seller=customers_without_seller,
                             sort_by='grouped',
                             show_customers_without_calls=show_customers_without_calls)
    
    elif sort_by == 'by_calls':
        # Sort by number of calls (descending)
        customers_query = Customer.query.options(
            db.joinedload(Customer.seller),
            db.joinedload(Customer.territory),
            db.joinedload(Customer.notes)
        ).outerjoin(Note).group_by(Customer.id).order_by(
            func.count(Note.id).desc(),
            Customer.name
        )
        
        # Filter out customers without calls if preference is False
        if not show_customers_without_calls:
            customers = [c for c in customers_query.all() if len(c.notes) > 0]
        else:
            customers = customers_query.all()
        
        return render_template('customers_list.html', customers=customers, sort_by='by_calls', show_customers_without_calls=show_customers_without_calls)
    
    else:
        # Alphabetical view (default)
        customers_query = Customer.query.options(
            db.joinedload(Customer.seller),
            db.joinedload(Customer.territory),
            db.joinedload(Customer.notes)
        ).order_by(Customer.name)
        
        # Filter out customers without calls if preference is False
        if not show_customers_without_calls:
            customers = [c for c in customers_query.all() if len(c.notes) > 0]
        else:
            customers = customers_query.all()
        
        return render_template('customers_list.html', customers=customers, sort_by='alphabetical', show_customers_without_calls=show_customers_without_calls)


@customers_bp.route('/customer/new', methods=['GET', 'POST'])
def customer_create():
    """Create a new customer (FR003, FR031)."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        nickname = request.form.get('nickname', '').strip()
        tpid = request.form.get('tpid', '').strip()
        tpid_url = request.form.get('tpid_url', '').strip()
        seller_id = request.form.get('seller_id')
        territory_id = request.form.get('territory_id')
        referrer = request.form.get('referrer', '')
        
        if not name:
            flash('Customer name is required.', 'danger')
            return redirect(url_for('customers.customer_create'))
        
        if not tpid:
            flash('TPID is required.', 'danger')
            return redirect(url_for('customers.customer_create'))
        
        try:
            tpid_value = int(tpid)
        except ValueError:
            flash('TPID must be a valid number.', 'danger')
            return redirect(url_for('customers.customer_create'))
        
        customer = Customer(
            name=name,
            nickname=nickname if nickname else None,
            tpid=tpid_value,
            tpid_url=tpid_url if tpid_url else None,
            seller_id=int(seller_id) if seller_id else None,
            territory_id=int(territory_id) if territory_id else None)
        db.session.add(customer)
        db.session.commit()
        
        flash(f'Customer "{name}" created successfully!', 'success')
        
        # Redirect back to referrer (FR031)
        if referrer:
            return redirect(referrer)
        
        return redirect(url_for('customers.customer_view', id=customer.id))
    
    sellers = Seller.query.order_by(Seller.name).all()
    territories = Territory.query.order_by(Territory.name).all()
    
    # Pre-select seller and territory from query params (FR032)
    preselect_seller_id = request.args.get('seller_id', type=int)
    preselect_territory_id = request.args.get('territory_id', type=int)
    
    # If seller is pre-selected and has exactly one territory, auto-select it
    if preselect_seller_id:
        seller = Seller.query.filter_by(id=preselect_seller_id).first()
        if seller and len(seller.territories) == 1:
            preselect_territory_id = seller.territories[0].id
    
    # If territory is pre-selected and has only one seller, auto-select it (FR032)
    if preselect_territory_id and not preselect_seller_id:
        territory = Territory.query.filter_by(id=preselect_territory_id).first()
        if territory:
            # territory.sellers is already a list from eager loading
            territory_sellers = territory.sellers
            if len(territory_sellers) == 1:
                preselect_seller_id = territory_sellers[0].id
    
    # Capture referrer for redirect after creation (FR031)
    referrer = request.referrer or ''
    
    return render_template('customer_form.html', 
                         customer=None, 
                         sellers=sellers, 
                         territories=territories,
                         preselect_seller_id=preselect_seller_id,
                         preselect_territory_id=preselect_territory_id,
                         referrer=referrer)


@customers_bp.route('/customer/<int:id>')
def customer_view(id):
    """View customer details with engagement dashboard (FR008)."""
    customer = Customer.query.filter_by(id=id).first_or_404()
    # Sort call logs by date (descending) - customer.notes is already loaded as a list
    notes = sorted(customer.notes, key=lambda c: c.call_date, reverse=True)
    
    # Get revenue analysis for this customer if available
    from app.models import RevenueAnalysis, Engagement
    revenue_analyses = RevenueAnalysis.query.filter_by(
        customer_name=customer.name
    ).order_by(RevenueAnalysis.priority_score.desc()).all()
    
    # Compute engagement metrics
    engagements = customer.engagements  # already ordered by created_at desc
    active_engagements = [e for e in engagements if e.status == 'Active']

    # Identify unassigned notes (not linked to any engagement)
    assigned_note_ids = set()
    for eng in engagements:
        for note in eng.notes:
            assigned_note_ids.add(note.id)
    unassigned_notes = [n for n in notes if n.id not in assigned_note_ids]

    # Compute last contact date
    last_contact = notes[0].call_date if notes else None

    # Active milestone count
    active_milestones = customer.milestones.filter(
        ~db.or_(
            db.literal_column('msx_status').in_(['Completed', 'Cancelled', 'Lost to Competitor', 'Hygiene/Duplicate']),
        )
    ).count() if customer.milestones.count() > 0 else 0

    # Opportunity count
    opportunity_count = customer.opportunities.count()

    return render_template('customer_view.html', 
                          customer=customer, 
                          notes=notes,
                          engagements=engagements,
                          active_engagements=active_engagements,
                          unassigned_notes=unassigned_notes,
                          revenue_analyses=revenue_analyses,
                          last_contact=last_contact,
                          active_milestone_count=active_milestones,
                          opportunity_count=opportunity_count)


@customers_bp.route('/customer/<int:id>/edit', methods=['GET', 'POST'])
def customer_edit(id):
    """Edit customer (FR008)."""
    customer = Customer.query.filter_by(id=id).first_or_404()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        nickname = request.form.get('nickname', '').strip()
        tpid = request.form.get('tpid', '').strip()
        tpid_url = request.form.get('tpid_url', '').strip()
        seller_id = request.form.get('seller_id')
        territory_id = request.form.get('territory_id')
        
        if not name:
            flash('Customer name is required.', 'danger')
            return redirect(url_for('customers.customer_edit', id=id))
        
        if not tpid:
            flash('TPID is required.', 'danger')
            return redirect(url_for('customers.customer_edit', id=id))
        
        try:
            tpid_value = int(tpid)
        except ValueError:
            flash('TPID must be a valid number.', 'danger')
            return redirect(url_for('customers.customer_edit', id=id))
        
        customer.name = name
        customer.nickname = nickname if nickname else None
        customer.tpid = tpid_value
        customer.tpid_url = tpid_url if tpid_url else None
        customer.seller_id = int(seller_id) if seller_id else None
        customer.territory_id = int(territory_id) if territory_id else None

        csam_id = request.form.get('csam_id', '').strip()
        if csam_id:
            csam_id_int = int(csam_id)
            available_ids = {c.id for c in customer.available_csams}
            if csam_id_int in available_ids:
                customer.csam_id = csam_id_int
        else:
            customer.csam_id = None

        db.session.commit()

        # Trigger backup to include updated customer data in OneDrive JSON
        try:
            _backup_customer(customer.id)
        except Exception:
            pass  # Backup failure should not block customer edit
        
        flash(f'Customer "{name}" updated successfully!', 'success')
        return redirect(url_for('customers.customer_view', id=customer.id))
    
    sellers = Seller.query.order_by(Seller.name).all()
    territories = Territory.query.order_by(Territory.name).all()
    
    return render_template('customer_form.html', 
                         customer=customer, 
                         sellers=sellers, 
                         territories=territories,
                         referrer='')


@customers_bp.route('/api/customer/<int:customer_id>/tpid-url', methods=['POST'])
def api_save_tpid_url(customer_id):
    """
    API endpoint to save a single customer's MSX Account URL.
    Used by auto-fill to save immediately on confident matches.
    
    Expected JSON: { "tpid_url": "https://..." }
    """
    try:
        customer = db.session.get(Customer, customer_id)
        if not customer:
            return jsonify({"success": False, "error": "Customer not found"}), 404
        
        data = request.get_json()
        if not data or not data.get('tpid_url'):
            return jsonify({"success": False, "error": "No tpid_url provided"}), 400
        
        tpid_url = data['tpid_url'].strip()
        customer.tpid_url = tpid_url
        db.session.commit()
        
        return jsonify({
            "success": True,
            "customer_id": customer_id,
            "customer_name": customer.name
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@customers_bp.route('/customer/<int:id>/csam', methods=['POST'])
def customer_update_csam(id):
    """Update customer's selected primary CSAM via AJAX."""
    customer = Customer.query.filter_by(id=id).first_or_404()
    data = request.get_json(silent=True) or {}
    csam_id = data.get('csam_id')

    if csam_id is not None and csam_id != '':
        csam_id = int(csam_id)
        # Validate the CSAM is in this customer's available list
        available_ids = {c.id for c in customer.available_csams}
        if csam_id not in available_ids:
            return jsonify({'success': False, 'error': 'Invalid CSAM selection'}), 400
        customer.csam_id = csam_id
    else:
        customer.csam_id = None

    db.session.commit()
    return jsonify({'success': True, 'csam_id': customer.csam_id})


@customers_bp.route('/customer/<int:id>/overview', methods=['POST'])
def customer_update_overview(id):
    """Update customer account context via AJAX or form POST."""
    customer = Customer.query.filter_by(id=id).first_or_404()
    
    account_context = request.form.get('account_context', '').strip()
    customer.account_context = account_context if account_context else None
    
    db.session.commit()

    # Trigger backup to include updated context in OneDrive JSON
    try:
        _backup_customer(customer.id)
    except Exception:
        pass  # Backup failure should not block save
    
    # Check if AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'account_context': customer.account_context})
    
    flash('Account context updated successfully.', 'success')
    return redirect(url_for('customers.customer_view', id=id))


# API routes
@customers_bp.route('/api/customers', methods=['GET'])
def api_customers_list():
    """API endpoint for listing all customers (for quick create modal, fill my day, etc.)."""
    customers = Customer.query.options(
        db.joinedload(Customer.territory)
    ).order_by(Customer.name).all()
    
    results = [{
        'id': c.id,
        'name': c.name,
        'nickname': c.nickname,
        'tpid': c.tpid,
        'tpid_url': c.tpid_url,
        'territory': c.territory.name if c.territory else None
    } for c in customers]
    
    return jsonify(results), 200


@customers_bp.route('/api/customers/autocomplete', methods=['GET'])
def api_customers_autocomplete():
    """API endpoint for customer name autocomplete."""
    query = request.args.get('q', '').strip()
    
    if not query or len(query) < 2:
        return jsonify([]), 200
    
    # Search customers by name, nickname, or TPID (case-insensitive, contains)
    customers = Customer.query.filter(
        db.or_(
            Customer.name.ilike(f'%{query}%'),
            Customer.nickname.ilike(f'%{query}%'),
            Customer.tpid.cast(db.String).ilike(f'%{query}%')
        )
    ).order_by(Customer.name).limit(10).all()
    
    results = [{
        'id': c.id,
        'name': c.name,
        'nickname': c.nickname,
        'tpid': c.tpid
    } for c in customers]
    
    return jsonify(results), 200

