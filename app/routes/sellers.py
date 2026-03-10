"""
Seller routes for NoteHelper.
Handles seller listing, creation, viewing, and editing.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify
from datetime import date, datetime, timezone
from sqlalchemy.orm import joinedload, subqueryload

from app.models import db, Seller, Territory, Customer, Milestone, Engagement

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
    """View seller details — single source of truth for a seller."""
    seller = Seller.query.options(
        db.joinedload(Seller.customers).joinedload(Customer.notes)
    ).filter_by(id=id).first_or_404()
    
    # Get customers with their most recent note
    customers_data = []
    for customer in sorted(seller.customers, key=lambda c: c.name):
        sorted_notes = sorted(customer.notes, key=lambda c: c.call_date, reverse=True)
        most_recent_note = sorted_notes[0] if sorted_notes else None
        customers_data.append({
            'customer': customer,
            'last_note': most_recent_note
        })
    
    # Sort by most recent note date (nulls last)
    min_date = datetime.min
    def get_sort_key(x):
        if not x['last_note']:
            return min_date
        return x['last_note'].call_date
    customers_data.sort(key=get_sort_key, reverse=True)
    
    # Check if seller can be deleted (no associated customers)
    can_delete = len(seller.customers) == 0
    
    # Get revenue analysis for this seller's customers
    from app.services.revenue_analysis import get_seller_alerts
    revenue_alerts = get_seller_alerts(seller.name)
    
    # Get active milestones for this seller's customers (top 10 by monthly usage)
    customer_ids = [c.id for c in seller.customers]
    milestones_data = []
    if customer_ids:
        active_statuses = {'On Track', 'At Risk', 'Blocked'}
        milestones = (
            Milestone.query
            .filter(
                Milestone.customer_id.in_(customer_ids),
                Milestone.msx_status.in_(active_statuses)
            )
            .options(
                db.joinedload(Milestone.customer),
                db.joinedload(Milestone.opportunity),
            )
            .all()
        )
        
        now = datetime.now(timezone.utc)
        for ms in milestones:
            days_until = None
            if ms.due_date:
                due = ms.due_date if ms.due_date.tzinfo else ms.due_date.replace(
                    tzinfo=timezone.utc
                )
                days_until = (due - now).days
            
            milestones_data.append({
                'id': ms.id,
                'title': ms.display_text,
                'status': ms.msx_status,
                'urgency': ms.due_date_urgency,
                'customer_name': ms.customer.get_display_name() if ms.customer else 'Unknown',
                'customer_id': ms.customer.id if ms.customer else None,
                'due_date': ms.due_date,
                'days_until_due': days_until,
                'monthly_usage': ms.monthly_usage,
                'workload': ms.workload,
                'url': ms.url,
            })
        
        # Sort by due date ascending (closest to due first, nulls last)
        milestones_data.sort(
            key=lambda x: x['due_date'] if x['due_date'] else datetime.max
        )
    
    return render_template(
        'seller_view.html',
        seller=seller,
        customers=customers_data,
        can_delete=can_delete,
        revenue_alerts=revenue_alerts,
        milestones=milestones_data,
    )


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


@sellers_bp.route('/api/seller/<int:id>/engagements')
def api_seller_engagements(id):
    """Return active/on-hold engagements for a specific seller's customers."""
    seller = Seller.query.filter_by(id=id).first_or_404()
    customer_ids = [c.id for c in seller.customers]

    if not customer_ids:
        return jsonify({'success': True, 'engagements': [], 'count': 0})

    status_filter = request.args.get('status', '').strip()

    query = Engagement.query.filter(
        Engagement.customer_id.in_(customer_ids),
        Engagement.status.in_(['Active', 'On Hold'])
    )
    if status_filter in ('Active', 'On Hold'):
        query = query.filter(Engagement.status == status_filter)

    query = query.options(
        joinedload(Engagement.customer),
        subqueryload(Engagement.notes),
        subqueryload(Engagement.opportunities),
        subqueryload(Engagement.milestones),
    )

    engagements = query.order_by(Engagement.updated_at.desc()).all()

    results = []
    for eng in engagements:
        results.append({
            'id': eng.id,
            'title': eng.title,
            'status': eng.status,
            'customer_name': eng.customer.name if eng.customer else 'Unknown',
            'customer_id': eng.customer_id,
            'customer_favicon': (eng.customer.favicon_b64
                                if eng.customer and eng.customer.favicon_b64
                                else None),
            'estimated_acr': eng.estimated_acr,
            'target_date': eng.target_date.isoformat() if eng.target_date else None,
            'story_completeness': eng.story_completeness,
            'linked_note_count': eng.linked_note_count,
            'opportunity_count': len(eng.opportunities),
            'milestone_count': len(eng.milestones),
            'updated_at': eng.updated_at.isoformat() if eng.updated_at else None,
        })

    return jsonify({'success': True, 'engagements': results, 'count': len(results)})

