"""
Seller routes for NoteHelper.
Handles seller listing, creation, viewing, and editing.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from datetime import date, datetime

from app.models import db, Seller, Territory, Customer

# Create blueprint
sellers_bp = Blueprint('sellers', __name__)


@sellers_bp.route('/sellers')
def sellers_list():
    """List all sellers."""
    sellers = Seller.query.options(
        db.joinedload(Seller.territories).joinedload(Territory.pod),
        db.joinedload(Seller.customers)
    ).order_by(Seller.name).all()
    return render_template('sellers_list.html', sellers=sellers)


@sellers_bp.route('/seller/new', methods=['GET', 'POST'])
def seller_create():
    """Create a new seller (FR002)."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        alias = request.form.get('alias', '').strip().replace('@microsoft.com', '') or None
        seller_type = request.form.get('seller_type', 'Growth')
        territory_ids = request.form.getlist('territory_ids')
        
        if not name:
            flash('Seller name is required.', 'danger')
            return redirect(url_for('sellers.seller_create'))
        
        # Check for duplicate
        existing = Seller.query.filter_by(name=name).first()
        if existing:
            flash(f'Seller "{name}" already exists.', 'warning')
            return redirect(url_for('sellers.seller_view', id=existing.id))
        
        seller = Seller(name=name, alias=alias, seller_type=seller_type)
        
        # Add territories to many-to-many relationship
        if territory_ids:
            for territory_id in territory_ids:
                territory = db.session.get(Territory, int(territory_id))
                if territory:
                    seller.territories.append(territory)
        
        db.session.add(seller)
        db.session.commit()
        
        flash(f'Seller "{name}" created successfully!', 'success')
        return redirect(url_for('sellers.sellers_list'))
    
    territories = Territory.query.order_by(Territory.name).all()
    existing_sellers = Seller.query.order_by(Seller.name).all()
    return render_template('seller_form.html', seller=None, territories=territories, existing_sellers=existing_sellers)


@sellers_bp.route('/seller/<int:id>')
def seller_view(id):
    """View seller details (FR007)."""
    seller = Seller.query.options(
        db.joinedload(Seller.customers).joinedload(Customer.call_logs)
    ).filter_by(id=id).first_or_404()
    
    # Get customers with their most recent call log
    customers_data = []
    for customer in sorted(seller.customers, key=lambda c: c.name):
        # Get most recent call log (sort in-memory since already loaded)
        sorted_calls = sorted(customer.call_logs, key=lambda c: c.call_date, reverse=True)
        most_recent_call = sorted_calls[0] if sorted_calls else None
        customers_data.append({
            'customer': customer,
            'last_call': most_recent_call
        })
    
    # Sort by most recent call date (nulls last)
    min_date = datetime.min
    def get_sort_key(x):
        if not x['last_call']:
            return min_date
        return x['last_call'].call_date
    customers_data.sort(key=get_sort_key, reverse=True)
    
    # Check if seller can be deleted (no associated customers)
    can_delete = len(seller.customers) == 0
    
    # Get revenue analysis for this seller's customers
    from app.services.revenue_analysis import get_seller_alerts
    revenue_alerts = get_seller_alerts(seller.name)
    
    return render_template('seller_view.html', seller=seller, customers=customers_data, 
                          can_delete=can_delete, revenue_alerts=revenue_alerts)


@sellers_bp.route('/seller/<int:id>/edit', methods=['GET', 'POST'])
def seller_edit(id):
    """Edit seller (FR007)."""
    seller = Seller.query.filter_by(id=id).first_or_404()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        alias = request.form.get('alias', '').strip().replace('@microsoft.com', '') or None
        seller_type = request.form.get('seller_type', 'Growth')
        territory_ids = request.form.getlist('territory_ids')
        
        if not name:
            flash('Seller name is required.', 'danger')
            return redirect(url_for('sellers.seller_edit', id=id))
        
        # Check for duplicate (excluding current seller)
        existing = Seller.query.filter(
            Seller.name == name,
            Seller.id != id
        ).first()
        if existing:
            flash(f'Seller "{name}" already exists.', 'warning')
            return redirect(url_for('sellers.seller_edit', id=id))
        
        seller.name = name
        seller.alias = alias
        seller.seller_type = seller_type
        
        # Update territories - replace the collection
        seller.territories = []
        if territory_ids:
            for territory_id in territory_ids:
                territory = db.session.get(Territory, int(territory_id))
                if territory:
                    seller.territories.append(territory)
        
        db.session.commit()
        
        flash(f'Seller "{name}" updated successfully!', 'success')
        return redirect(url_for('sellers.seller_view', id=seller.id))
    
    territories = Territory.query.order_by(Territory.name).all()
    existing_sellers = Seller.query.filter(Seller.id != id).order_by(Seller.name).all()
    return render_template('seller_form.html', seller=seller, territories=territories, existing_sellers=existing_sellers)


@sellers_bp.route('/seller/create-inline', methods=['POST'])
def seller_create_inline():
    """Create seller inline from other forms."""
    name = request.form.get('name', '').strip()
    redirect_to = request.form.get('redirect_to', url_for('sellers.sellers_list'))
    
    if name:
        existing = Seller.query.filter_by(name=name).first()
        if not existing:
            seller = Seller(name=name)
            db.session.add(seller)
            db.session.commit()
            flash(f'Seller "{name}" created successfully!', 'success')
        else:
            flash(f'Seller "{name}" already exists.', 'info')
    
    return redirect(redirect_to)


@sellers_bp.route('/seller/<int:id>/delete', methods=['POST'])
def seller_delete(id):
    """Delete seller if it has no associated customers."""
    seller = Seller.query.filter_by(id=id).first_or_404()
    seller_name = seller.name
    
    # Check if seller has any customers
    if len(seller.customers) > 0:
        flash(f'Cannot delete seller "{seller_name}" because it has {len(seller.customers)} associated customer(s).', 'danger')
        return redirect(url_for('sellers.seller_view', id=id))
    
    # Delete the seller
    db.session.delete(seller)
    db.session.commit()
    
    flash(f'Seller "{seller_name}" deleted successfully.', 'success')
    return redirect(url_for('sellers.sellers_list'))

