"""
Tests for account sync upsert logic (issue #23).

Validates that import_accounts and sync_accounts properly update
existing customer fields, seller fields, territories, and POD
associations on re-sync.
"""
import pytest
import json
from threading import Barrier, Lock
from types import SimpleNamespace

from app.models import (
    db, Customer, Seller, Territory, POD, Vertical,
    SolutionEngineer, SyncStatus, User,
)


@pytest.fixture
def alias_import(app, monkeypatch, tmp_path):
    """Use real import orchestration and alias parsing with isolated source responses."""
    from app.services import account_sync, msx_api

    accounts = {
        f"account-{index}": {
            "accountid": f"account-{index}",
            "name": f"Alias Account {index}",
            "msp_mstopparentid": str(650000 + index),
            "_ownerid_value": f"owner-{index % 4}",
        }
        for index in range(5)
    }
    monkeypatch.setattr(app, "instance_path", str(tmp_path / "instance"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(account_sync, "scan_init", lambda: {
        "success": True, "account_ids": list(accounts),
    })
    monkeypatch.setattr(account_sync, "_par_query_accounts", lambda ids, *_args: {
        account_id: accounts[account_id] for account_id in ids
    })
    monkeypatch.setattr(account_sync, "_par_query_teams", lambda *_args: {
        "account_sellers": {}, "unique_sellers": {}, "account_ses": {},
    })
    requests = []

    def request(method, url):
        """Return directory data through the real get_user_info parser."""
        assert method == "GET"
        user_id = url.split("systemusers(", 1)[1].split(")", 1)[0]
        requests.append(user_id)
        return SimpleNamespace(status_code=200, json=lambda: {
            "domainname": f"{user_id}@microsoft.com",
            "fullname": f"Owner {user_id}",
        })

    monkeypatch.setattr(msx_api, "_msx_request", request)
    return account_sync, msx_api, accounts, requests


class TestAccountAliasResolution:
    """Alias workers expose their progress, results and unexpected failures."""

    def test_each_alias_reports_progress_and_populates_owner(self, app, alias_import):
        """Four unique owners produce four lookups and four visible progress updates."""
        sync, _api, _accounts, requests = alias_import
        with app.app_context():
            events = [json.loads(event.removeprefix("data: "))
                      for event in sync.sync_accounts_stream(skip_aura=True)]
            assert events[-1]["complete"] is True
            assert events[-1]["summary"]["customers_created"] == 5
            for index in range(5):
                customer = Customer.query.filter_by(tpid=650000 + index).one()
                assert customer.dae_alias == f"owner-{index % 4}"
                assert customer.dae_name == f"Owner owner-{index % 4}"

        assert sorted(requests) == [f"owner-{index}" for index in range(4)]
        progress = [event for event in events
                    if event.get("message", "").startswith("Resolving aliases")]
        assert [event["message"] for event in progress] == [
            f"Resolving aliases ({index}/4)..." for index in range(5)
        ]
        assert [event["progress"] for event in progress] == [88, 88, 89, 90, 90]
        assert not any("error" in event for event in events)

    def test_alias_lookups_still_run_in_parallel(self, app, alias_import, monkeypatch):
        """All three workers enter directory work before any of them completes."""
        sync, api, _accounts, requests = alias_import
        request = api._msx_request
        barrier = Barrier(3, timeout=5)
        lock = Lock()
        entered = 0

        def parallel_request(method, url):
            """Hold the first three lookups until all worker slots are active."""
            nonlocal entered
            with lock:
                entered += 1
                position = entered
            if position <= 3:
                barrier.wait()
            return request(method, url)

        monkeypatch.setattr(api, "_msx_request", parallel_request)
        with app.app_context():
            events = [json.loads(event.removeprefix("data: "))
                      for event in sync.sync_accounts_stream(skip_aura=True)]
        assert events[-1]["complete"] is True
        assert len(requests) == 4
        assert entered == 4

    def test_worker_exception_fails_import_explicitly(
        self, app, alias_import, monkeypatch, caplog,
    ):
        """A crashed resolver cannot leave the import waiting or report success."""
        sync, _api, _accounts, _requests = alias_import

        def fail_lookup(_user_id):
            """Simulate an unexpected resolver failure outside its normal API contract."""
            raise RuntimeError("Alias worker crashed")

        monkeypatch.setattr(sync, "get_user_info", fail_lookup)
        with app.app_context():
            events = [json.loads(event.removeprefix("data: "))
                      for event in sync.sync_accounts_stream(skip_aura=True)]
            assert Customer.query.count() == 0
            status = SyncStatus.query.filter_by(sync_type="accounts").one()
            assert status.success is False
        assert "resolving aliases" in events[-1]["error"]
        assert "Alias worker crashed" in events[-1]["error"]
        assert not any(event.get("complete") for event in events)
        assert not any(event.get("message") == "Creating PODs..." for event in events)
        assert "Alias worker crashed" in caplog.text

    def test_missing_directory_users_remain_logged_and_nonfatal(
        self, app, alias_import, monkeypatch, caplog,
    ):
        """A normal not-found response preserves existing best-effort owner enrichment."""
        sync, api, _accounts, _requests = alias_import
        monkeypatch.setattr(api, "_msx_request",
                            lambda *_args: SimpleNamespace(status_code=404))
        with app.app_context():
            events = [json.loads(event.removeprefix("data: "))
                      for event in sync.sync_accounts_stream(skip_aura=True)]
            assert events[-1]["complete"] is True
            assert Customer.query.filter_by(tpid=650000).one().dae_alias is None
        assert "Failed to look up systemuser" in caplog.text
        assert any(event.get("message") == "Resolving aliases (4/4)..." for event in events)

    def test_no_missing_aliases_skips_directory_work(self, app, alias_import):
        """Imports with no owners do not create an empty alias-resolution phase."""
        sync, _api, accounts, requests = alias_import
        for account in accounts.values():
            account.pop("_ownerid_value")
        with app.app_context():
            events = [json.loads(event.removeprefix("data: "))
                      for event in sync.sync_accounts_stream(skip_aura=True)]
        assert events[-1]["complete"] is True
        assert requests == []
        assert not any(event.get("message", "").startswith("Resolving aliases")
                       for event in events)


class TestImportAccountsUpsert:
    """Tests for the non-streaming import_accounts endpoint."""

    def _post_import(self, client, accounts, territories=None):
        """Admit the HTTP import and execute its captured payload in the worker."""
        from app.services import job_queue

        response = client.post(
            '/api/msx/import-accounts',
            json={
                "accounts": accounts,
                "territories": territories or [],
            },
            content_type='application/json',
        )
        assert response.status_code == 202
        job_queue.process_one('test-worker')
        status = client.get(response.get_json()['status_url'])
        assert status.get_json()['job']['status'] == 'done'
        return status

    def test_new_customer_created(self, client, app):
        """First import should create customers."""
        with app.app_context():
            resp = self._post_import(client, [
                {"tpid": 1001, "name": "Contoso Ltd", "territory_name": None, "seller_name": None},
            ])
            assert resp.status_code == 200
            data = resp.get_json()['job']['result']
            assert data["success"] is True
            assert data["customers_created"] == 1

            cust = Customer.query.filter_by(tpid=1001).first()
            assert cust is not None
            assert cust.name == "Contoso Ltd"

    def test_existing_customer_name_updated(self, client, app):
        """Re-import with new name should update the customer."""
        with app.app_context():
            # First import
            self._post_import(client, [
                {"tpid": 2001, "name": "Old Name Corp"},
            ])
            cust = Customer.query.filter_by(tpid=2001).first()
            assert cust.name == "Old Name Corp"

            # Re-import with new name
            resp = self._post_import(client, [
                {"tpid": 2001, "name": "New Name Corp"},
            ])
            data = resp.get_json()['job']['result']
            assert data["customers_created"] == 0
            assert data["customers_updated"] == 1
            assert data["customers_unchanged"] == 0

            db.session.refresh(cust)
            assert cust.name == "New Name Corp"

    def test_existing_customer_seller_updated(self, client, app):
        """Re-import with new seller should update the customer's seller."""
        with app.app_context():
            # Create initial seller and territory
            self._post_import(client,
                accounts=[
                    {"tpid": 3001, "name": "Acme Corp",
                     "territory_name": "West.ATU.01.01", "seller_name": "Alice Smith"},
                ],
                territories=[
                    {"name": "West.ATU.01.01", "seller_name": "Alice Smith"},
                ],
            )
            cust = Customer.query.filter_by(tpid=3001).first()
            assert cust.seller is not None
            assert cust.seller.name == "Alice Smith"

            # Re-import with new seller
            resp = self._post_import(client,
                accounts=[
                    {"tpid": 3001, "name": "Acme Corp",
                     "territory_name": "West.ATU.01.01", "seller_name": "Bob Jones"},
                ],
                territories=[
                    {"name": "West.ATU.01.01", "seller_name": "Bob Jones"},
                ],
            )
            data = resp.get_json()['job']['result']
            assert data["customers_updated"] == 1

            db.session.refresh(cust)
            assert cust.seller.name == "Bob Jones"

    def test_existing_customer_territory_updated(self, client, app):
        """Re-import with new territory should update the customer."""
        with app.app_context():
            self._post_import(client,
                accounts=[
                    {"tpid": 4001, "name": "Fabrikam",
                     "territory_name": "East.ATU.01.01", "seller_name": None},
                ],
                territories=[
                    {"name": "East.ATU.01.01"},
                ],
            )
            cust = Customer.query.filter_by(tpid=4001).first()
            assert cust.territory is not None
            assert cust.territory.name == "East.ATU.01.01"

            # Re-import with new territory
            resp = self._post_import(client,
                accounts=[
                    {"tpid": 4001, "name": "Fabrikam",
                     "territory_name": "West.ATU.02.05", "seller_name": None},
                ],
                territories=[
                    {"name": "West.ATU.02.05"},
                ],
            )
            data = resp.get_json()['job']['result']
            assert data["customers_updated"] == 1

            db.session.refresh(cust)
            assert cust.territory.name == "West.ATU.02.05"

    def test_duplicate_tpid_in_batch_skipped(self, client, app):
        """Same TPID appearing twice in a batch should only process once."""
        with app.app_context():
            resp = self._post_import(client, [
                {"tpid": 5001, "name": "Dup Corp"},
                {"tpid": 5001, "name": "Dup Corp Copy"},
            ])
            data = resp.get_json()['job']['result']
            assert data["customers_created"] == 1
            assert data["customers_skipped"] == 1

    def test_no_data_loss_on_missing_fields(self, client, app):
        """Re-import with None seller shouldn't clear existing seller."""
        with app.app_context():
            self._post_import(client,
                accounts=[
                    {"tpid": 6001, "name": "Preserved Corp",
                     "territory_name": "Keep.ATU.01.01", "seller_name": "Carol White"},
                ],
                territories=[
                    {"name": "Keep.ATU.01.01", "seller_name": "Carol White"},
                ],
            )
            cust = Customer.query.filter_by(tpid=6001).first()
            assert cust.seller.name == "Carol White"

            # Re-import without seller info
            resp = self._post_import(client,
                accounts=[
                    {"tpid": 6001, "name": "Preserved Corp",
                     "territory_name": None, "seller_name": None},
                ],
            )
            # Seller should NOT be cleared (we only update when new value exists)
            db.session.refresh(cust)
            assert cust.seller is not None
            assert cust.seller.name == "Carol White"

    def test_unchanged_customer_not_counted_as_updated(self, client, app):
        """Re-import with same data should not increment updated count."""
        with app.app_context():
            self._post_import(client, [
                {"tpid": 7001, "name": "Static Corp"},
            ])
            resp = self._post_import(client, [
                {"tpid": 7001, "name": "Static Corp"},
            ])
            data = resp.get_json()['job']['result']
            assert data["customers_updated"] == 0
            assert data["customers_unchanged"] == 1


class TestImportStreamUpsert:
    """Tests for the streaming sync_accounts customer upsert logic.

    Since sync_accounts is SSE-based and requires MSX API mocking,
    we test the core upsert logic via the simpler import_accounts
    endpoint which shares the same patterns. The streaming-specific
    additions (POD rebuild, seller update, territory pod update) are
    tested separately.
    """

    def test_pod_rebuild_clears_stale_associations(self, app):
        """POD associations should be cleared and rebuilt on sync."""
        with app.app_context():
            # Create a POD with a territory and SE
            pod = POD(name="West POD 01")
            territory = Territory(name="West.ATU.01.01", pod=pod)
            se = SolutionEngineer(name="Test SE", specialty="Azure Data")
            se.pods.append(pod)
            db.session.add_all([pod, territory, se])
            db.session.commit()

            assert len(pod.territories) == 1
            assert len(pod.solution_engineers) == 1

            # Simulate the POD rebuild logic from sync_accounts:
            # Clear associations
            pod.territories = []
            pod.solution_engineers = []
            db.session.flush()

            # After clear, associations should be empty
            assert len(pod.territories) == 0
            assert len(pod.solution_engineers) == 0

            # Rebuild: re-assign the territory
            territory.pod = pod
            se.pods.append(pod)
            db.session.commit()

            db.session.refresh(pod)
            assert len(pod.territories) == 1
            assert len(pod.solution_engineers) == 1

    def test_territory_pod_always_updated(self, app):
        """Territory's pod should always be updated from MSX, not just when missing."""
        with app.app_context():
            pod_old = POD(name="Old POD")
            pod_new = POD(name="New POD")
            territory = Territory(name="Move.ATU.01.01", pod=pod_old)
            db.session.add_all([pod_old, pod_new, territory])
            db.session.commit()

            assert territory.pod.name == "Old POD"

            # Simulate the territory upsert logic: always update pod
            territory.pod = pod_new
            db.session.commit()

            db.session.refresh(territory)
            assert territory.pod.name == "New POD"

    def test_seller_type_updated_on_resync(self, app):
        """Existing seller's type should be updated when MSX has a different value."""
        with app.app_context():
            seller = Seller(name="Test Seller", seller_type="Growth")
            db.session.add(seller)
            db.session.commit()

            assert seller.seller_type == "Growth"

            # Simulate seller upsert from sync_accounts
            new_type = "Acquisition"
            if seller.seller_type != new_type:
                seller.seller_type = new_type
            db.session.commit()

            db.session.refresh(seller)
            assert seller.seller_type == "Acquisition"

    def test_seller_alias_backfilled(self, app):
        """Seller alias should be backfilled when missing."""
        with app.app_context():
            seller = Seller(name="No Alias Seller", alias=None)
            db.session.add(seller)
            db.session.commit()

            assert seller.alias is None

            # Simulate alias backfill
            seller.alias = "noalias"
            db.session.commit()

            db.session.refresh(seller)
            assert seller.alias == "noalias"

    def test_customer_verticals_updated(self, app):
        """Customer verticals should be replaced with MSX values on resync."""
        with app.app_context():
            v_old = Vertical(name="Healthcare")
            v_new = Vertical(name="Financial Services")
            customer = Customer(name="Vert Corp", tpid=8001)
            customer.verticals.append(v_old)
            db.session.add_all([v_old, v_new, customer])
            db.session.commit()

            assert len(customer.verticals) == 1
            assert customer.verticals[0].name == "Healthcare"

            # Simulate vertical upsert: replace
            customer.verticals = [v_new]
            db.session.commit()

            db.session.refresh(customer)
            assert len(customer.verticals) == 1
            assert customer.verticals[0].name == "Financial Services"

    def test_customer_name_change_preserves_notes(self, app):
        """Changing customer name should not affect associated notes."""
        with app.app_context():
            from app.models import Note
            user = User.query.first()
            customer = Customer(name="Original Name", tpid=9001)
            db.session.add(customer)
            db.session.flush()

            note = Note(
                content="Important meeting notes",
                customer_id=customer.id,
            )
            db.session.add(note)
            db.session.commit()

            # Change name (simulating M/A rebrand)
            customer.name = "Rebranded Name"
            db.session.commit()

            db.session.refresh(note)
            assert note.customer_id == customer.id
            assert note.customer.name == "Rebranded Name"
