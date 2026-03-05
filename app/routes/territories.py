"""
Territory routes for NoteHelper.
Handles territory listing, creation, viewing, and editing.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from datetime import timedelta
from sqlalchemy import func

from app.models import db, Territory, Seller, Customer, CallLog, POD, UserPreference, utc_now

# Create blueprint
territories_bp = Blueprint('territories', __name__)


@territories_bp.route('/territories')
def territories_list():
    """List all territories."""
    territories = Territory.query.options(
        db.joinedload(Territory.sellers),
        db.joinedload(Territory.customers),
        db.joinedload(Territory.pod)
    ).order_by(Territory.name).all()
    return render_template('territories_list.html', territories=territories)


@territories_bp.route('/territory/new', methods=['GET', 'POST'])
def territory_create():
    """Create a new territory (FR001)."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        
        if not name:
            flash('Territory name is required.', 'danger')
            return redirect(url_for('territories.territory_create'))
        
        # Check for duplicate
        existing = Territory.query.filter_by(name=name).first()
        if existing:
            flash(f'Territory "{name}" already exists.', 'warning')
            return redirect(url_for('territories.territory_view', id=existing.id))
        
        territory = Territory(name=name)
        db.session.add(territory)
        db.session.commit()
        
        flash(f'Territory "{name}" created successfully!', 'success')
        return redirect(url_for('territories.territories_list'))
    
    # Show existing territories to prevent duplicates
    existing_territories = Territory.query.order_by(Territory.name).all()
    return render_template('territory_form.html', territory=None, existing_territories=existing_territories)


@territories_bp.route('/territory/<int:id>')
def territory_view(id):
    """View territory details (FR006)."""
    territory = Territory.query.options(
        db.joinedload(Territory.pod)
    ).filter_by(id=id).first_or_404()
    # Sort sellers in-memory since they're eager-loaded
    sellers = sorted(territory.sellers, key=lambda s: s.name)
    
    # Get user preference for territory view
    pref = UserPreference.query.first()
    show_accounts = pref.territory_view_accounts if pref else False
    
    recent_calls = []
    growth_customers = []
    acquisition_customers = []
    
    if show_accounts:
        # Get all customers in this territory with call counts
        customers_with_counts = db.session.query(
            Customer,
            func.count(CallLog.id).label('call_count')
        ).join(
            Seller, Customer.seller_id == Seller.id
        ).filter(
            Seller.territories.any(Territory.id == id)
        ).outerjoin(
            CallLog, Customer.id == CallLog.customer_id
        ).group_by(Customer.id, Seller.id, Seller.seller_type).all()
        
        # Group by seller type and sort by call count
        for customer, call_count in customers_with_counts:
            customer.call_count = call_count  # Attach count for template
            # Determine customer type by their seller
            if customer.seller:
                if customer.seller.seller_type == 'Growth':
                    growth_customers.append(customer)
                elif customer.seller.seller_type == 'Acquisition':
                    acquisition_customers.append(customer)
        
        # Sort by call count descending
        growth_customers.sort(key=lambda c: c.call_count, reverse=True)
        acquisition_customers.sort(key=lambda c: c.call_count, reverse=True)
    else:
        # Get calls from last 7 days
        week_ago = utc_now() - timedelta(days=7)
        recent_calls = CallLog.query.join(Customer).filter(
            Customer.territory_id == id,
            CallLog.call_date >= week_ago
        ).order_by(CallLog.call_date.desc()).all()
    
    return render_template('territory_view.html', 
                         territory=territory, 
                         sellers=sellers, 
                         recent_calls=recent_calls,
                         show_accounts=show_accounts,
                         growth_customers=growth_customers,
                         acquisition_customers=acquisition_customers)


@territories_bp.route('/territory/<int:id>/edit', methods=['GET', 'POST'])
def territory_edit(id):
    """Edit territory (FR006)."""
    territory = Territory.query.filter_by(id=id).first_or_404()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        
        if not name:
            flash('Territory name is required.', 'danger')
            return redirect(url_for('territories.territory_edit', id=id))
        
        # Check for duplicate (excluding current territory)
        existing = Territory.query.filter(
            Territory.name == name,
            Territory.id != id
        ).first()
        if existing:
            flash(f'Territory "{name}" already exists.', 'warning')
            return redirect(url_for('territories.territory_edit', id=id))
        
        territory.name = name
        db.session.commit()
        
        flash(f'Territory "{name}" updated successfully!', 'success')
        return redirect(url_for('territories.territory_view', id=territory.id))
    
    existing_territories = Territory.query.filter(Territory.id != id).order_by(Territory.name).all()
    return render_template('territory_form.html', territory=territory, existing_territories=existing_territories)


@territories_bp.route('/territory/create-inline', methods=['POST'])
def territory_create_inline():
    """Create territory inline from other forms."""
    name = request.form.get('name', '').strip()
    redirect_to = request.form.get('redirect_to', url_for('territories.territories_list'))
    
    if name:
        existing = Territory.query.filter_by(name=name).first()
        if not existing:
            territory = Territory(name=name)
            db.session.add(territory)
            db.session.commit()
            flash(f'Territory "{name}" created successfully!', 'success')
        else:
            flash(f'Territory "{name}" already exists.', 'info')
    
    return redirect(redirect_to)

