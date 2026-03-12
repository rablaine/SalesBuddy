"""
POD routes for NoteHelper.
Handles POD listing, viewing, and editing.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from app.models import db, POD, Territory, SolutionEngineer, TerritoryDSSSelection

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

    # Build territory -> specialty -> {dss_list, selected_id} for dropdown UI
    # Load existing selections for territories in this pod
    terr_ids = [t.id for t in territories]
    selections = TerritoryDSSSelection.query.filter(
        TerritoryDSSSelection.territory_id.in_(terr_ids)
    ).all() if terr_ids else []
    sel_map = {(s.territory_id, s.specialty): s.solution_engineer_id for s in selections}

    territory_dss_grouped = {}
    for territory in territories:
        dss_list = [
            se for se in territory.solution_engineers
            if se.specialty and se.specialty not in cloud_specialties
        ]
        if not dss_list:
            continue
        specialties = {}
        for se in dss_list:
            specialties.setdefault(se.specialty, []).append(se)
        grouped = {}
        for spec in sorted(specialties.keys()):
            grouped[spec] = {
                "dss_list": sorted(specialties[spec], key=lambda s: s.name),
                "selected_id": sel_map.get((territory.id, spec)),
            }
        territory_dss_grouped[territory] = grouped
    
    return render_template('pod_view.html',
                         pod=pod,
                         territories=territories,
                         sellers=sellers,
                         solution_engineers=solution_engineers,
                         territory_dss=territory_dss,
                         territory_dss_grouped=territory_dss_grouped)


@pods_bp.route('/pod/<int:id>/edit', methods=['GET'])
def pod_edit(id):
    """Edit POD DSS assignments (name, territories, SEs managed by MSX sync)."""
    pod = POD.query.options(
        db.selectinload(POD.territories),
        db.selectinload(POD.solution_engineers)
    ).filter_by(id=id).first_or_404()
    
    # Build DSS grouped data for edit form (same structure as pod_view)
    cloud_specialties = {"Azure Data", "Azure Core and Infra", "Azure Apps and AI"}
    terr_ids = [t.id for t in pod.territories]
    selections = TerritoryDSSSelection.query.filter(
        TerritoryDSSSelection.territory_id.in_(terr_ids)
    ).all() if terr_ids else []
    sel_map = {(s.territory_id, s.specialty): s.solution_engineer_id for s in selections}

    territory_dss_grouped = {}
    for territory in sorted(pod.territories, key=lambda t: t.name):
        dss_list = [
            se for se in territory.solution_engineers
            if se.specialty and se.specialty not in cloud_specialties
        ]
        if not dss_list:
            continue
        specialties = {}
        for se in dss_list:
            specialties.setdefault(se.specialty, []).append(se)
        grouped = {}
        for spec in sorted(specialties.keys()):
            grouped[spec] = {
                "dss_list": sorted(specialties[spec], key=lambda s: s.name),
                "selected_id": sel_map.get((territory.id, spec)),
            }
        territory_dss_grouped[territory] = grouped
    
    return render_template('pod_form.html', pod=pod,
                           territory_dss_grouped=territory_dss_grouped)


@pods_bp.route('/territory/<int:territory_id>/dss-selection', methods=['POST'])
def territory_update_dss(territory_id):
    """Update the selected DSS for a territory + specialty via AJAX."""
    territory = Territory.query.filter_by(id=territory_id).first_or_404()
    data = request.get_json(silent=True) or {}
    specialty = data.get('specialty', '').strip()
    se_id = data.get('solution_engineer_id')

    if not specialty:
        return jsonify({'success': False, 'error': 'Specialty is required'}), 400

    existing = TerritoryDSSSelection.query.filter_by(
        territory_id=territory.id, specialty=specialty
    ).first()

    if se_id is not None and se_id != '' and se_id != 'none':
        se_id = int(se_id)
        # Validate the SE is linked to this territory with this specialty
        valid = SolutionEngineer.query.filter_by(id=se_id, specialty=specialty).first()
        if not valid or territory not in valid.territories:
            return jsonify({'success': False, 'error': 'Invalid DSS selection'}), 400
        if existing:
            existing.solution_engineer_id = se_id
        else:
            sel = TerritoryDSSSelection(
                territory_id=territory.id,
                specialty=specialty,
                solution_engineer_id=se_id
            )
            db.session.add(sel)
    else:
        # Clear selection
        if existing:
            db.session.delete(existing)

    db.session.commit()
    return jsonify({'success': True, 'solution_engineer_id': se_id if se_id not in (None, '', 'none') else None})

