"""
Tests for DSS (Digital Solution Specialist) features.
Covers: SolutionEngineer territory M2M, DSS grouping on pod view,
filtering cloud SEs vs DSSs, batch_query_account_dss API, and template rendering.
"""
import pytest
from unittest.mock import patch, MagicMock
from app.models import db, SolutionEngineer, Territory, POD, TerritoryDSSSelection


@pytest.fixture
def dss_data(app, sample_data):
    """Create DSS and Cloud SE test data linked to territories and pod."""
    with app.app_context():
        t1 = db.session.get(Territory, sample_data['territory1_id'])
        t2 = db.session.get(Territory, sample_data['territory2_id'])

        # Create a POD and link territories
        pod = POD(name='Test POD')
        db.session.add(pod)
        db.session.flush()
        pod.territories.extend([t1, t2])

        # Cloud SEs (pod-based, should NOT appear in DSS grouping)
        cloud_se1 = SolutionEngineer(name='Cloud Alice', alias='clouda', specialty='Azure Data')
        cloud_se2 = SolutionEngineer(name='Cloud Bob', alias='cloudb', specialty='Azure Core and Infra')
        db.session.add_all([cloud_se1, cloud_se2])
        db.session.flush()
        pod.solution_engineers.extend([cloud_se1, cloud_se2])
        # Also link a cloud SE to a territory to test exclusion
        t1.solution_engineers.append(cloud_se1)

        # DSSs (territory-based, should appear in DSS grouping)
        dss1 = SolutionEngineer(name='DSS Carol', alias='dssc', specialty='Security')
        dss2 = SolutionEngineer(name='DSS Dave', alias='dssd', specialty='Modern Work')
        dss3 = SolutionEngineer(name='DSS Eve', alias='dsse', specialty='Azure Infrastructure')
        db.session.add_all([dss1, dss2, dss3])
        db.session.flush()
        t1.solution_engineers.extend([dss1, dss2])
        t2.solution_engineers.append(dss3)

        db.session.commit()

        return {
            **sample_data,
            'pod_id': pod.id,
            'cloud_se1_id': cloud_se1.id,
            'cloud_se2_id': cloud_se2.id,
            'dss1_id': dss1.id,
            'dss2_id': dss2.id,
            'dss3_id': dss3.id,
        }


class TestDSSModel:
    """Tests for SolutionEngineer model with DSS territory relationships."""

    def test_dss_creation(self, app):
        """Test creating a DSS SolutionEngineer record."""
        with app.app_context():
            dss = SolutionEngineer(name='Test DSS', alias='testd', specialty='Security')
            db.session.add(dss)
            db.session.commit()

            fetched = db.session.get(SolutionEngineer, dss.id)
            assert fetched.name == 'Test DSS'
            assert fetched.alias == 'testd'
            assert fetched.specialty == 'Security'
            assert fetched.created_at is not None

    def test_dss_get_email(self, app):
        """Test email generation from alias."""
        with app.app_context():
            dss = SolutionEngineer(name='Test', alias='testd')
            assert dss.get_email() == 'testd@microsoft.com'

    def test_dss_get_email_no_alias(self, app):
        """Test email returns None when no alias."""
        with app.app_context():
            dss = SolutionEngineer(name='Test')
            assert dss.get_email() is None

    def test_territory_m2m_relationship(self, app, dss_data):
        """Test M2M between SolutionEngineer and Territory."""
        with app.app_context():
            dss = db.session.get(SolutionEngineer, dss_data['dss1_id'])
            assert len(dss.territories) == 1
            assert dss.territories[0].name == 'West Region'

    def test_territory_m2m_reverse(self, app, dss_data):
        """Test reverse M2M from Territory to SolutionEngineers."""
        with app.app_context():
            t1 = db.session.get(Territory, dss_data['territory1_id'])
            se_names = {se.name for se in t1.solution_engineers}
            # t1 has cloud_se1 + dss1 + dss2
            assert 'DSS Carol' in se_names
            assert 'DSS Dave' in se_names
            assert 'Cloud Alice' in se_names

    def test_dss_multiple_territories(self, app):
        """Test a DSS can be linked to multiple territories."""
        with app.app_context():
            t1 = Territory(name='Region A')
            t2 = Territory(name='Region B')
            dss = SolutionEngineer(name='Multi DSS', specialty='Security')
            db.session.add_all([t1, t2, dss])
            db.session.flush()
            dss.territories.extend([t1, t2])
            db.session.commit()

            fetched = db.session.get(SolutionEngineer, dss.id)
            assert len(fetched.territories) == 2
            terr_names = {t.name for t in fetched.territories}
            assert terr_names == {'Region A', 'Region B'}


class TestDSSPodGrouping:
    """Tests for DSS territory grouping on pod view page."""

    def test_pod_view_renders_dss_card(self, client, dss_data):
        """Test pod view renders the DSS card when DSSs exist."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        assert resp.status_code == 200
        assert b'Digital Solution Specialists' in resp.data

    def test_pod_view_dss_grouped_by_territory(self, client, dss_data):
        """Test DSSs are grouped under territory headers."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        assert 'West Region' in html
        assert 'East Region' in html
        assert 'DSS Carol' in html
        assert 'DSS Dave' in html
        assert 'DSS Eve' in html

    def test_pod_view_dss_shows_specialty_badge(self, client, dss_data):
        """Test DSS specialty appears as a badge."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        assert 'Security' in html
        assert 'Modern Work' in html
        assert 'Azure Infrastructure' in html

    def test_pod_view_dss_shows_dropdown_for_specialty(self, client, dss_data):
        """Test DSS section shows dropdown when multiple DSSs share a specialty."""
        with client.application.app_context():
            # Add a second Security DSS to territory1 so dropdown appears
            t1 = db.session.get(Territory, dss_data['territory1_id'])
            dss_extra = SolutionEngineer(name='DSS Frank', alias='dssf', specialty='Security')
            db.session.add(dss_extra)
            db.session.flush()
            t1.solution_engineers.append(dss_extra)
            db.session.commit()
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        assert 'Select Security' in html
        assert 'DSS Carol' in html
        assert 'DSS Frank' in html

    def test_pod_view_single_dss_shows_badge(self, client, dss_data):
        """Test single DSS per specialty shows as badge, not dropdown."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        # Modern Work has only DSS Dave - should show as badge
        assert 'DSS Dave' in html
        # Should NOT show a dropdown for Modern Work
        assert 'Select Modern Work' not in html

    def test_pod_view_excludes_cloud_ses_from_dss(self, client, dss_data):
        """Test cloud SEs do NOT appear in the DSS card section."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        # The DSS card uses bi-diagram-2 icon; find that section
        dss_section_start = html.find('Digital Solution Specialists')
        assert dss_section_start != -1
        dss_section = html[dss_section_start:]
        # Cloud Alice is linked to territory1 but has "Azure Data" specialty
        assert 'Cloud Alice' not in dss_section

    def test_pod_view_cloud_ses_still_in_se_card(self, client, dss_data):
        """Test cloud SEs still appear in the regular Solution Engineers card."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        # Cloud SEs are linked to pod via solution_engineers_pods
        assert 'Cloud Alice' in html
        assert 'Cloud Bob' in html

    def test_pod_view_no_dss_card_when_empty(self, app, client, sample_data):
        """Test DSS card is not rendered when no DSSs are linked."""
        with app.app_context():
            pod = POD(name='Empty POD')
            db.session.add(pod)
            db.session.commit()
            pod_id = pod.id
        resp = client.get(f'/pod/{pod_id}')
        assert resp.status_code == 200
        assert b'Digital Solution Specialists' not in resp.data


class TestDSSSelection:
    """Tests for DSS specialty selection AJAX endpoint."""

    def test_save_dss_selection(self, app, client, dss_data):
        """Test saving a DSS selection for a territory + specialty."""
        resp = client.post(
            f'/territory/{dss_data["territory1_id"]}/dss-selection',
            json={'specialty': 'Security', 'solution_engineer_id': dss_data['dss1_id']},
            headers={'X-Requested-With': 'XMLHttpRequest'}
        )
        assert resp.status_code == 200
        assert resp.json['success'] is True
        with app.app_context():
            sel = TerritoryDSSSelection.query.filter_by(
                territory_id=dss_data['territory1_id'], specialty='Security'
            ).first()
            assert sel is not None
            assert sel.solution_engineer_id == dss_data['dss1_id']

    def test_update_dss_selection(self, app, client, dss_data):
        """Test updating an existing DSS selection."""
        with app.app_context():
            # Add a second Security DSS
            t1 = db.session.get(Territory, dss_data['territory1_id'])
            dss_extra = SolutionEngineer(name='DSS Gary', alias='dssg', specialty='Security')
            db.session.add(dss_extra)
            db.session.flush()
            t1.solution_engineers.append(dss_extra)
            # Set initial selection
            sel = TerritoryDSSSelection(
                territory_id=dss_data['territory1_id'],
                specialty='Security',
                solution_engineer_id=dss_data['dss1_id']
            )
            db.session.add(sel)
            db.session.commit()
            extra_id = dss_extra.id
        # Update to the new DSS
        resp = client.post(
            f'/territory/{dss_data["territory1_id"]}/dss-selection',
            json={'specialty': 'Security', 'solution_engineer_id': extra_id},
            headers={'X-Requested-With': 'XMLHttpRequest'}
        )
        assert resp.status_code == 200
        assert resp.json['success'] is True
        with app.app_context():
            sel = TerritoryDSSSelection.query.filter_by(
                territory_id=dss_data['territory1_id'], specialty='Security'
            ).first()
            assert sel.solution_engineer_id == extra_id

    def test_clear_dss_selection(self, app, client, dss_data):
        """Test clearing a DSS selection."""
        with app.app_context():
            sel = TerritoryDSSSelection(
                territory_id=dss_data['territory1_id'],
                specialty='Security',
                solution_engineer_id=dss_data['dss1_id']
            )
            db.session.add(sel)
            db.session.commit()
        resp = client.post(
            f'/territory/{dss_data["territory1_id"]}/dss-selection',
            json={'specialty': 'Security', 'solution_engineer_id': 'none'},
            headers={'X-Requested-With': 'XMLHttpRequest'}
        )
        assert resp.status_code == 200
        with app.app_context():
            sel = TerritoryDSSSelection.query.filter_by(
                territory_id=dss_data['territory1_id'], specialty='Security'
            ).first()
            assert sel is None

    def test_invalid_dss_selection(self, client, dss_data):
        """Test selecting a DSS not linked to the territory."""
        resp = client.post(
            f'/territory/{dss_data["territory1_id"]}/dss-selection',
            json={'specialty': 'Security', 'solution_engineer_id': dss_data['dss3_id']},
            headers={'X-Requested-With': 'XMLHttpRequest'}
        )
        assert resp.status_code == 400
        assert resp.json['success'] is False

    def test_missing_specialty(self, client, dss_data):
        """Test request without specialty returns 400."""
        resp = client.post(
            f'/territory/{dss_data["territory1_id"]}/dss-selection',
            json={'solution_engineer_id': dss_data['dss1_id']},
            headers={'X-Requested-With': 'XMLHttpRequest'}
        )
        assert resp.status_code == 400

    def test_pod_view_shows_selected_badge(self, app, client, dss_data):
        """Test pod view shows badge for selected DSS instead of dropdown."""
        with app.app_context():
            # Add second Security DSS so it would normally show dropdown
            t1 = db.session.get(Territory, dss_data['territory1_id'])
            dss_extra = SolutionEngineer(name='DSS Hank', alias='dssh', specialty='Security')
            db.session.add(dss_extra)
            db.session.flush()
            t1.solution_engineers.append(dss_extra)
            # Set selection
            sel = TerritoryDSSSelection(
                territory_id=dss_data['territory1_id'],
                specialty='Security',
                solution_engineer_id=dss_data['dss1_id']
            )
            db.session.add(sel)
            db.session.commit()
        resp = client.get(f'/pod/{dss_data["pod_id"]}')
        html = resp.data.decode()
        # Should show badge, not dropdown
        assert 'Select Security' not in html
        assert 'DSS Carol' in html  # badge text

    def test_pod_edit_shows_dss_dropdowns(self, client, dss_data):
        """Test edit form shows DSS specialty dropdowns."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}/edit')
        html = resp.data.decode()
        assert 'Digital Solution Specialist Assignments' in html
        assert 'Security' in html
        assert 'DSS Carol' in html

    def test_pod_edit_readonly_fields(self, client, dss_data):
        """Test edit form shows name, territories, SEs as read-only."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}/edit')
        html = resp.data.decode()
        # Name should be plain text, not an input
        assert 'name="name"' not in html
        assert 'Test POD' in html
        # No territory/SE checkboxes (no name="territory_ids" or name="se_ids")
        assert 'name="territory_ids"' not in html
        assert 'name="se_ids"' not in html
        assert 'Managed by MSX sync' in html

    def test_pod_edit_single_dss_shows_badge(self, client, dss_data):
        """Test edit form shows badge for single DSS per specialty."""
        resp = client.get(f'/pod/{dss_data["pod_id"]}/edit')
        html = resp.data.decode()
        # Modern Work has only DSS Dave — should show as badge, not dropdown
        assert 'DSS Dave' in html
        assert 'Select Modern Work' not in html


class TestBatchQueryAccountDSS:
    """Tests for the batch_query_account_dss API function."""

    @patch('app.services.msx_api.query_entity')
    def test_basic_dss_query(self, mock_query):
        """Test basic DSS query returns structured data."""
        mock_query.return_value = {
            'success': True,
            'records': [
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'Jane DSS',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-1',
                },
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'John DSS',
                    'msp_qualifier2': 'Modern Work',
                    '_msp_systemuserid_value': 'user-2',
                },
            ],
        }

        from app.services.msx_api import batch_query_account_dss
        result = batch_query_account_dss(['acct-1'])

        assert result['success'] is True
        assert 'acct-1' in result['account_dss']
        assert len(result['account_dss']['acct-1']) == 2
        assert result['unique_dss']['Jane DSS']['specialty'] == 'Security'
        assert result['unique_dss']['John DSS']['specialty'] == 'Modern Work'

    @patch('app.services.msx_api.query_entity')
    def test_deduplicates_within_account(self, mock_query):
        """Test duplicate DSS names within one account are deduplicated."""
        mock_query.return_value = {
            'success': True,
            'records': [
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'Same Person',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-1',
                },
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'Same Person',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-1',
                },
            ],
        }

        from app.services.msx_api import batch_query_account_dss
        result = batch_query_account_dss(['acct-1'])

        assert len(result['account_dss']['acct-1']) == 1

    @patch('app.services.msx_api.query_entity')
    def test_multiple_accounts(self, mock_query):
        """Test DSS query across multiple account IDs."""
        mock_query.return_value = {
            'success': True,
            'records': [
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'Alice DSS',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-1',
                },
                {
                    '_msp_accountid_value': 'acct-2',
                    'msp_fullname': 'Bob DSS',
                    'msp_qualifier2': 'Modern Work',
                    '_msp_systemuserid_value': 'user-2',
                },
            ],
        }

        from app.services.msx_api import batch_query_account_dss
        result = batch_query_account_dss(['acct-1', 'acct-2'])

        assert 'acct-1' in result['account_dss']
        assert 'acct-2' in result['account_dss']
        assert len(result['unique_dss']) == 2

    @patch('app.services.msx_api.query_entity')
    def test_failed_query_continues(self, mock_query):
        """Test that a failed batch still returns partial results."""
        mock_query.side_effect = [
            {'success': False, 'error': 'timeout'},
            {
                'success': True,
                'records': [
                    {
                        '_msp_accountid_value': 'acct-2',
                        'msp_fullname': 'Alice DSS',
                        'msp_qualifier2': 'Security',
                        '_msp_systemuserid_value': 'user-1',
                    },
                ],
            },
        ]

        from app.services.msx_api import batch_query_account_dss
        # batch_size=1 forces two separate batches
        result = batch_query_account_dss(['acct-1', 'acct-2'], batch_size=1)

        assert result['success'] is True
        assert 'acct-1' not in result['account_dss']
        assert 'acct-2' in result['account_dss']

    @patch('app.services.msx_api.query_entity')
    def test_empty_account_list(self, mock_query):
        """Test empty input returns empty results."""
        from app.services.msx_api import batch_query_account_dss
        result = batch_query_account_dss([])

        assert result['success'] is True
        assert result['account_dss'] == {}
        assert result['unique_dss'] == {}
        mock_query.assert_not_called()

    @patch('app.services.msx_api.query_entity')
    def test_skips_records_without_name(self, mock_query):
        """Test records with empty/missing name are skipped."""
        mock_query.return_value = {
            'success': True,
            'records': [
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': '',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-1',
                },
                {
                    '_msp_accountid_value': 'acct-1',
                    'msp_fullname': 'Valid Person',
                    'msp_qualifier2': 'Security',
                    '_msp_systemuserid_value': 'user-2',
                },
            ],
        }

        from app.services.msx_api import batch_query_account_dss
        result = batch_query_account_dss(['acct-1'])

        assert len(result['account_dss']['acct-1']) == 1
        assert result['account_dss']['acct-1'][0]['name'] == 'Valid Person'
