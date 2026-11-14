"""
Regression test for the milestone-sync connectivity guard.

When MSX is unreachable (e.g. off-VPN), the streaming sync used to sail through
the write phase and report every active milestone as "deactivated" (the scary
"690 deactivated" run). It must instead abort with a vpn_blocked event and touch
nothing.
"""
from unittest.mock import patch

import pytest

from app.models import db, Customer, Milestone
from app.services import milestone_sync


def test_stream_aborts_on_wholesale_fetch_failure(app, sample_data):
    """Fetch failure aborts before writes without guessing that VPN caused it."""
    with app.app_context():
        cust = Customer.query.filter(
            Customer.tpid_url.isnot(None), Customer.tpid_url != ''
        ).first()

        ms = Milestone(
            url='https://example.com/ms',
            title='Keep me active',
            msx_milestone_id='guid-keepme-guard',
            msx_status='On Track',
            customer_id=cust.id,
            on_my_team=False,
        )
        db.session.add(ms)
        db.session.commit()
        ms_id = ms.id

        # Simulate off-VPN: account id extracts fine, but every opportunity
        # fetch fails. The preflight connectivity check passes (this test
        # targets the per-phase wholesale-failure guard, not the preflight).
        with patch.object(milestone_sync, 'test_connection',
                          return_value={'success': True}), \
                patch.object(milestone_sync, 'extract_account_id_from_url',
                          return_value='acct-123'), \
                patch.object(milestone_sync, 'batch_get_opportunities',
                             return_value={'success': False}):
            events = list(milestone_sync.sync_all_customer_milestones_stream())

        joined = '\n'.join(events)
        assert 'event: error' in joined
        assert 'event: vpn_blocked' not in joined
        assert 'event: complete' not in joined

        # The active milestone was NOT touched.
        refreshed = db.session.get(Milestone, ms_id)
        assert refreshed.last_synced_at is None
        assert refreshed.msx_status == 'On Track'


@pytest.mark.parametrize('failed_stage', ['opportunities', 'milestones'])
def test_partial_fetch_failure_aborts_before_any_customer_writes(app, failed_stage):
    """One failed chunk must not turn into empty successful customer imports."""
    with app.app_context():
        for index in range(21):
            db.session.add(Customer(
                name=f'Chunk customer {index}', tpid=91000 + index,
                tpid_url=f'https://example.test/account-{index:02}',
            ))
        db.session.commit()

        def opportunities(accounts):
            """Produce enough opportunities for two milestone chunks."""
            if failed_stage == 'opportunities' and 'account-00' in accounts:
                return {'success': False, 'error': 'Simulated opportunity fetch failure'}
            return {'success': True, 'by_account': {
                account: [{'id': f'opp-{account}', 'name': account}] for account in accounts
            }}

        with patch.object(milestone_sync, 'test_connection', return_value={'success': True}), \
             patch.object(milestone_sync, 'extract_account_id_from_url',
                          side_effect=lambda url: url.rsplit('/', 1)[-1]), \
             patch.object(milestone_sync, 'batch_get_opportunities',
                          side_effect=opportunities), \
             patch.object(milestone_sync, 'batch_get_milestones', side_effect=[
                 {'success': False, 'error': 'Simulated milestone fetch failure'},
                 {'success': True, 'by_opportunity': {}},
             ]), \
             patch.object(milestone_sync, '_apply_customer_milestones') as apply:
            events = ''.join(milestone_sync.sync_all_customer_milestones_stream())

        assert 'event: error' in events
        assert 'event: complete' not in events
        assert 'Simulated' in events
        apply.assert_not_called()


def test_stream_bails_immediately_when_preflight_detects_vpn(app, sample_data):
    """Off-VPN preflight -> vpn_blocked before any batch fetch is dispatched."""
    with app.app_context():
        cust = Customer.query.filter(
            Customer.tpid_url.isnot(None), Customer.tpid_url != ''
        ).first()

        ms = Milestone(
            url='https://example.com/ms',
            title='Keep me active',
            msx_milestone_id='guid-keepme-preflight',
            msx_status='On Track',
            customer_id=cust.id,
            on_my_team=False,
        )
        db.session.add(ms)
        db.session.commit()
        ms_id = ms.id

        # Preflight reports VPN blocked. The batch fetch must never run.
        with patch.object(milestone_sync, 'test_connection',
                          return_value={'success': False, 'vpn_blocked': True}), \
                patch.object(milestone_sync, 'batch_get_opportunities') as mock_batch:
            events = list(milestone_sync.sync_all_customer_milestones_stream())

        joined = '\n'.join(events)
        assert 'event: vpn_blocked' in joined
        assert 'event: complete' not in joined
        # We bailed on the single preflight call - no batches dispatched.
        mock_batch.assert_not_called()

        refreshed = db.session.get(Milestone, ms_id)
        assert refreshed.last_synced_at is None
        assert refreshed.msx_status == 'On Track'
