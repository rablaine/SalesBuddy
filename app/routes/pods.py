"""
POD routes for NoteHelper.
Handles POD listing, viewing, and editing.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.models import db, POD, Territory, SolutionEngineer

# Create blueprint
pods_bp = Blueprint('pods', __name__)


@pods_bp.route('/pods')
def pods_list():
    """List all PODs."""
    pods = POD.query.options(
        db.joinedload(POD.territories),
        db.joinedload(POD.solution_engineers)
    ).order_by(POD.name).all()
    return render_template('pods_list.html', pods=pods)


@pods_bp.route('/pod/<int:id>')
def pod_view(id):
    """View POD details with territories, sellers, and solution engineers."""
    # Use selectinload for better performance with collections
    pod = POD.query.options(
        db.selectinload(POD.territories).selectinload(Territory.sellers),
        db.selectinload(POD.territories).selectinload(Territory.solution_engineers),
        db.selectinload(POD.solution_engineers)
    ).filter_by(id=id).first_or_404()
    
    # Get all sellers from all territories in this POD
    sellers = set()
    for territory in pod.territories:
        for seller in territory.sellers:
            sellers.add(seller)
    sellers = sorted(list(sellers), key=lambda s: s.name)
    
    # Sort territories and solution engineers
    territories = sorted(pod.territories, key=lambda t: t.name)
    solution_engineers = sorted(pod.solution_engineers, key=lambda se: se.name)
    
    # Build territory -> DSSs grouping.
    # DSSs are SolutionEngineers linked to territories (not pods).
    # Cloud SEs (Azure Data, Azure Core and Infra, Azure Apps and AI) are pod-based.
    cloud_specialties = {"Azure Data", "Azure Core and Infra", "Azure Apps and AI"}
    territory_dss = {}
    for territory in territories:
        dss_list = [
            se for se in territory.solution_engineers
            if se.specialty not in cloud_specialties
        ]
        if dss_list:
            territory_dss[territory.name] = sorted(dss_list, key=lambda se: se.name)
    
    return render_template('pod_view.html',
                         pod=pod,
                         territories=territories,
                         sellers=sellers,
                         solution_engineers=solution_engineers,
                         territory_dss=territory_dss)


@pods_bp.route('/pod/<int:id>/edit', methods=['GET', 'POST'])
def pod_edit(id):
    """Edit POD with territories, sellers, and solution engineers."""
    pod = POD.query.options(
        db.selectinload(POD.territories),
        db.selectinload(POD.solution_engineers)
    ).filter_by(id=id).first_or_404()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        territory_ids = request.form.getlist('territory_ids')
        se_ids = request.form.getlist('se_ids')
        
        if not name:
            flash('POD name is required.', 'danger')
            return redirect(url_for('pods.pod_edit', id=id))
        
        # Check for duplicate
        existing = POD.query.filter(POD.name == name, POD.id != id).first()
        if existing:
            flash(f'POD "{name}" already exists.', 'warning')
            return redirect(url_for('pods.pod_view', id=existing.id))
        
        pod.name = name
        
        # Update territories
        pod.territories.clear()
        for territory_id in territory_ids:
            territory = db.session.get(Territory, int(territory_id))
            if territory:
                pod.territories.append(territory)
        
        # Update solution engineers
        pod.solution_engineers.clear()
        for se_id in se_ids:
            se = db.session.get(SolutionEngineer, int(se_id))
            if se:
                pod.solution_engineers.append(se)
        
        db.session.commit()
        
        flash(f'POD "{name}" updated successfully!', 'success')
        return redirect(url_for('pods.pod_view', id=pod.id))
    
    # Get all territories and solution engineers for the form
    all_territories = Territory.query.options(
        db.selectinload(Territory.sellers)
    ).order_by(Territory.name).all()
    all_ses = SolutionEngineer.query.order_by(SolutionEngineer.name).all()
    
    return render_template('pod_form.html', pod=pod, all_territories=all_territories, all_ses=all_ses)

