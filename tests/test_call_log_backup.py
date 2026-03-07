"""
Tests for the file-based call log backup system.

Covers:
- backup.py service functions (_sanitize_folder_name, _customer_to_dict,
  backup_customer, backup_all_customers, restore_from_backup)
- backup routes (status, backup-all, restore)
- call_logs.py integration hooks
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import (
    CallLog,
    Customer,
    Partner,
    Seller,
    Territory,
    Topic,
    User,
    db,
)
from app.services.backup import (
    _customer_to_dict,
    _get_backup_root,
    _load_db_backup_config,
    _sanitize_folder_name,
    backup_all_customers,
    backup_customer,
    detect_onedrive_paths,
    find_backup_folder,
    get_auto_detected_backup_path,
    restore_all_from_folder,
    restore_from_backup,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def backup_dir():
    """Create a temporary directory for backups and clean up after."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def backup_config(app, backup_dir, monkeypatch):
    """Stub _load_db_backup_config to return a config pointing at the temp dir."""
    cfg_data = {"enabled": True, "backup_dir": backup_dir}
    monkeypatch.setattr(
        "app.services.backup._load_db_backup_config", lambda: cfg_data
    )
    yield cfg_data


@pytest.fixture
def customer_with_logs(app):
    """Create a customer with a seller, territory, call logs, topics, and partners."""
    with app.app_context():
        user = User.query.first()

        territory = Territory(name="Backup Territory")
        seller = Seller(
            name="Backup Seller", alias="bseller", seller_type="Growth"
        )
        db.session.add_all([territory, seller])
        db.session.flush()

        customer = Customer(
            name="Backup Test Customer",
            tpid=99999,
            seller_id=seller.id,
            territory_id=territory.id,
        )
        db.session.add(customer)
        db.session.flush()

        topic = Topic(name="Backup Topic")
        partner = Partner(name="Backup Partner")
        db.session.add_all([topic, partner])
        db.session.flush()

        cl = CallLog(
            customer_id=customer.id,
            call_date=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            content="Test call log for backup",
        )
        cl.topics.append(topic)
        cl.partners.append(partner)
        db.session.add(cl)
        db.session.commit()

        ids = {
            "customer_id": customer.id,
            "seller_id": seller.id,
            "territory_id": territory.id,
            "topic_id": topic.id,
            "partner_id": partner.id,
            "call_log_id": cl.id,
        }
        yield ids

        # Cleanup
        for cl_obj in CallLog.query.filter_by(customer_id=ids["customer_id"]).all():
            cl_obj.topics.clear()
            cl_obj.partners.clear()
        db.session.flush()
        CallLog.query.filter_by(customer_id=ids["customer_id"]).delete()
        Customer.query.filter_by(id=ids["customer_id"]).delete()
        Topic.query.filter_by(id=ids["topic_id"]).delete()
        Partner.query.filter_by(id=ids["partner_id"]).delete()
        Seller.query.filter_by(id=ids["seller_id"]).delete()
        Territory.query.filter_by(id=ids["territory_id"]).delete()
        db.session.commit()


# =========================================================================
# _sanitize_folder_name
# =========================================================================

class TestSanitizeFolderName:
    """Tests for _sanitize_folder_name utility."""

    def test_plain_name(self):
        assert _sanitize_folder_name("Alice Smith") == "Alice Smith"

    def test_strips_invalid_characters(self):
        assert _sanitize_folder_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"

    def test_collapses_whitespace(self):
        assert _sanitize_folder_name("Too   many   spaces") == "Too many spaces"

    def test_empty_string(self):
        assert _sanitize_folder_name("") == "_unnamed"

    def test_dot_only(self):
        assert _sanitize_folder_name(".") == "_unnamed"
        assert _sanitize_folder_name("..") == "_unnamed"


# =========================================================================
# _get_backup_root
# =========================================================================

class TestGetBackupRoot:
    """Tests for _get_backup_root config lookup."""

    def test_returns_none_when_no_config(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        with app.app_context():
            assert _get_backup_root() is None

    def test_returns_path_when_enabled(self, app, backup_config, backup_dir):
        with app.app_context():
            root = _get_backup_root()
            assert root is not None
            assert root == backup_dir

    def test_returns_none_when_disabled(self, app, backup_dir, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": backup_dir},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        with app.app_context():
            assert _get_backup_root() is None


# =========================================================================
# _customer_to_dict
# =========================================================================

class TestCustomerToDict:
    """Tests for customer serialization."""

    def test_serializes_customer_and_call_logs(self, app, customer_with_logs):
        with app.app_context():
            customer = Customer.query.get(customer_with_logs["customer_id"])
            result = _customer_to_dict(customer)

            assert result["_notehelper_backup"] is True
            assert result["_version"] == 2
            assert "_exported_at" in result

            cust = result["customer"]
            assert cust["name"] == "Backup Test Customer"
            assert cust["tpid"] == 99999
            assert cust["seller_name"] == "Backup Seller"
            assert cust["territory_name"] == "Backup Territory"

            assert len(result["call_logs"]) == 1
            cl = result["call_logs"][0]
            assert cl["content"] == "Test call log for backup"
            assert "Backup Topic" in cl["topics"]
            assert "Backup Partner" in cl["partners"]

    def test_handles_no_seller(self, app):
        with app.app_context():
            user = User.query.first()
            customer = Customer(
                name="No Seller Corp", tpid=77777
            )
            db.session.add(customer)
            db.session.flush()

            result = _customer_to_dict(customer)
            assert result["customer"]["seller_name"] is None
            assert result["call_logs"] == []

            db.session.delete(customer)
            db.session.commit()


# =========================================================================
# backup_customer
# =========================================================================

class TestBackupCustomer:
    """Tests for single-customer backup."""

    def test_writes_json_file(self, app, backup_config, customer_with_logs, backup_dir):
        with app.app_context():
            result = backup_customer(customer_with_logs["customer_id"])
            assert result is True

            filepath = os.path.join(backup_dir, "call_logs", "Backup Seller", "99999.json")
            assert os.path.isfile(filepath)

            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            assert data["_notehelper_backup"] is True
            assert data["customer"]["tpid"] == 99999

    def test_returns_false_when_disabled(self, app, customer_with_logs, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        with app.app_context():
            result = backup_customer(customer_with_logs["customer_id"])
            assert result is False

    def test_returns_false_for_missing_customer(self, app, backup_config):
        with app.app_context():
            result = backup_customer(999999)
            assert result is False

    def test_unassigned_seller_folder(self, app, backup_config, backup_dir):
        with app.app_context():
            user = User.query.first()
            customer = Customer(
                name="Orphan Corp", tpid=55555
            )
            db.session.add(customer)
            db.session.flush()
            cl = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                content="orphan log",
            )
            db.session.add(cl)
            db.session.commit()

            result = backup_customer(customer.id)
            assert result is True

            filepath = os.path.join(backup_dir, "call_logs", "Unassigned", "55555.json")
            assert os.path.isfile(filepath)

            db.session.delete(cl)
            db.session.delete(customer)
            db.session.commit()

    def test_overwrites_existing_file(self, app, backup_config, customer_with_logs, backup_dir):
        """Backup should overwrite the previous version of the file."""
        with app.app_context():
            backup_customer(customer_with_logs["customer_id"])
            filepath = os.path.join(backup_dir, "call_logs", "Backup Seller", "99999.json")
            first_mtime = os.path.getmtime(filepath)

            # Write again
            backup_customer(customer_with_logs["customer_id"])
            assert os.path.getmtime(filepath) >= first_mtime


# =========================================================================
# backup_all_customers
# =========================================================================

class TestBackupAllCustomers:
    """Tests for bulk backup."""

    def test_backs_up_all_customers_with_logs(self, app, backup_config, backup_dir):
        with app.app_context():
            user = User.query.first()
            seller = Seller(
                name="Bulk Seller", alias="bsell", seller_type="Growth"
            )
            db.session.add(seller)
            db.session.flush()

            for i in range(3):
                c = Customer(
                    name=f"Bulk Cust {i}",
                    tpid=80000 + i,
                    seller_id=seller.id,
                )
                db.session.add(c)
                db.session.flush()
                cl = CallLog(
                    customer_id=c.id,
                    call_date=datetime(2025, 1, 1 + i, tzinfo=timezone.utc),
                    content=f"Log {i}",
                )
                db.session.add(cl)
            db.session.commit()

            result = backup_all_customers()
            assert result["backed_up"] >= 3
            assert result["failed"] == 0

            for i in range(3):
                fp = os.path.join(backup_dir, "call_logs", "Bulk Seller", f"{80000 + i}.json")
                assert os.path.isfile(fp)

            # Cleanup
            for c in Customer.query.filter_by(seller_id=seller.id).all():
                CallLog.query.filter_by(customer_id=c.id).delete()
            Customer.query.filter_by(seller_id=seller.id).delete()
            db.session.delete(seller)
            db.session.commit()

    def test_returns_error_when_disabled(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        with app.app_context():
            result = backup_all_customers()
            assert result["backed_up"] == 0
            assert "error" in result


# =========================================================================
# restore_from_backup
# =========================================================================

class TestRestoreFromBackup:
    """Tests for DR restore."""

    def test_restores_call_logs_by_tpid(self, app):
        with app.app_context():
            from flask import g
            user = User.query.first()
            g.user = user

            customer = Customer(name="Restore Corp", tpid=44444)
            db.session.add(customer)
            db.session.commit()

            backup_data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {
                    "name": "Restore Corp",
                    "tpid": 44444,
                },
                "call_logs": [
                    {
                        "call_date": "2025-06-01T10:00:00+00:00",
                        "content": "Restored log 1",
                        "topics": ["Restored Topic"],
                        "partners": ["Restored Partner"],
                    },
                    {
                        "call_date": "2025-06-02T10:00:00+00:00",
                        "content": "Restored log 2",
                        "topics": [],
                        "partners": [],
                    },
                ],
            }

            result = restore_from_backup(backup_data)
            assert result["success"] is True
            assert result["logs_created"] == 2
            assert result["logs_skipped"] == 0
            assert result["customer_name"] == "Restore Corp"

            logs = CallLog.query.filter_by(customer_id=customer.id).all()
            assert len(logs) == 2

            topic = Topic.query.filter_by(name="Restored Topic").first()
            assert topic is not None

            # Cleanup
            for cl in logs:
                cl.topics.clear()
                cl.partners.clear()
            db.session.flush()
            CallLog.query.filter_by(customer_id=customer.id).delete()
            db.session.delete(customer)
            Topic.query.filter_by(name="Restored Topic").delete()
            Partner.query.filter_by(name="Restored Partner").delete()
            db.session.commit()

    def test_skips_duplicate_dates(self, app):
        with app.app_context():
            from flask import g
            user = User.query.first()
            g.user = user
            customer = Customer(name="Dup Corp", tpid=33333)
            db.session.add(customer)
            db.session.flush()

            existing_cl = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="Already exists",
            )
            db.session.add(existing_cl)
            db.session.commit()

            backup_data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {"tpid": 33333},
                "call_logs": [
                    {
                        "call_date": "2025-06-01T10:00:00+00:00",
                        "content": "Duplicate",
                        "topics": [],
                        "partners": [],
                    },
                    {
                        "call_date": "2025-06-02T10:00:00+00:00",
                        "content": "New one",
                        "topics": [],
                        "partners": [],
                    },
                ],
            }

            result = restore_from_backup(backup_data)
            assert result["success"] is True
            assert result["logs_created"] == 1
            assert result["logs_skipped"] == 1

            # Cleanup
            CallLog.query.filter_by(customer_id=customer.id).delete()
            db.session.delete(customer)
            db.session.commit()

    def test_rejects_invalid_payload(self, app):
        with app.app_context():
            result = restore_from_backup({"random": "data"})
            assert result["success"] is False

    def test_rejects_missing_tpid(self, app):
        with app.app_context():
            result = restore_from_backup({"_notehelper_backup": True, "customer": {}})
            assert result["success"] is False

    def test_rejects_unknown_tpid(self, app):
        with app.app_context():
            result = restore_from_backup({
                "_notehelper_backup": True,
                "customer": {"tpid": 111111},
                "call_logs": [],
            })
            assert result["success"] is False
            assert "not found" in result["error"]


# =========================================================================
# Route tests
# =========================================================================

class TestBackupRoutes:
    """Tests for backup API endpoints."""

    def test_status_when_disabled(self, client, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        resp = client.get("/api/backup/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is False

    def test_backup_all_when_disabled(self, client, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )
        resp = client.post("/api/backup/backup-all")
        assert resp.status_code == 400

    def test_backup_all_success(self, client, app, backup_dir, sample_data, monkeypatch):
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": True, "backup_dir": backup_dir},
        )

        resp = client.post("/api/backup/backup-all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["backed_up"] >= 1

    def test_restore_endpoint(self, client, app):
        with app.app_context():
            user = User.query.first()
            customer = Customer(name="API Restore", tpid=66666)
            db.session.add(customer)
            db.session.commit()
            customer_id = customer.id

        payload = {
            "_notehelper_backup": True,
            "_version": 2,
            "customer": {"tpid": 66666},
            "call_logs": [
                {
                    "call_date": "2025-07-01T10:00:00+00:00",
                    "content": "Via API",
                    "topics": [],
                    "partners": [],
                },
            ],
        }
        resp = client.post(
            "/api/backup/restore",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["logs_created"] == 1

        # Cleanup
        with app.app_context():
            CallLog.query.filter_by(customer_id=customer_id).delete()
            Customer.query.filter_by(id=customer_id).delete()
            db.session.commit()

    def test_restore_invalid_json(self, client):
        resp = client.post(
            "/api/backup/restore",
            data="not json",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_restore_unknown_tpid_returns_404(self, client):
        payload = {
            "_notehelper_backup": True,
            "customer": {"tpid": 999999},
            "call_logs": [],
        }
        resp = client.post(
            "/api/backup/restore",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_status_counts_files(self, client, app, backup_dir, monkeypatch):
        """Verify file_count reflects actual JSON files on disk."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": True, "backup_dir": backup_dir},
        )

        cl_dir = os.path.join(backup_dir, "call_logs", "Some Seller")
        os.makedirs(cl_dir, exist_ok=True)
        for i in range(3):
            Path(os.path.join(cl_dir, f"{i}.json")).write_text("{}")

        resp = client.get("/api/backup/status")
        data = resp.get_json()
        assert data["file_count"] == 3


# =========================================================================
# Integration: call_logs hooks trigger backup
# =========================================================================

class TestCallLogBackupHooks:
    """Verify that call log CRUD triggers a backup write."""

    def test_create_call_log_triggers_backup(self, client, app, backup_dir, sample_data, monkeypatch):
        """Creating a call log should write a backup file."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": True, "backup_dir": backup_dir},
        )

        resp = client.post(
            "/call-log/new",
            data={
                "customer_id": sample_data["customer1_id"],
                "call_date": "2025-08-01",
                "content": "Integration test log",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # customer1 has tpid=1001 and seller=Alice Smith
        filepath = os.path.join(backup_dir, "call_logs", "Alice Smith", "1001.json")
        assert os.path.isfile(filepath), f"Expected backup at {filepath}"

    def test_backup_not_written_when_disabled(self, client, app, backup_dir, sample_data, monkeypatch):
        """No backup file when feature is off."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None
        )

        client.post(
            "/call-log/new",
            data={
                "customer_id": sample_data["customer1_id"],
                "call_date": "2025-08-02",
                "content": "Should not trigger backup",
            },
            follow_redirects=True,
        )

        cl_dir = os.path.join(backup_dir, "call_logs")
        assert not os.path.exists(cl_dir)


# =========================================================================
# OneDrive auto-detection
# =========================================================================

class TestOneDriveDetection:
    """Test OneDrive folder detection logic."""

    @pytest.fixture(autouse=True)
    def _mock_registry(self, monkeypatch):
        """Prevent real Windows registry from leaking into tests."""
        def _no_reg(*args, **kwargs):
            raise OSError("mocked - no registry")
        monkeypatch.setattr("app.services.backup.winreg.OpenKey", _no_reg)

    def test_detect_from_env_var(self, tmp_path, monkeypatch):
        """OneDriveCommercial env var should be detected."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(od_path)
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.delenv("OneDrive", raising=False)
        # Clear USERPROFILE to avoid other detections
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        results = detect_onedrive_paths()
        assert len(results) >= 1
        assert results[0]["path"] == os.path.normpath(od_path)
        assert results[0]["source"] == "OneDriveCommercial env var"
        assert results[0]["has_backups"] is False

    def test_detect_with_existing_backups_folder(self, tmp_path, monkeypatch):
        """If Backups/NoteHelper exists, has_backups should be True."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(os.path.join(od_path, "Backups", "NoteHelper"))
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        results = detect_onedrive_paths()
        assert len(results) >= 1
        assert results[0]["has_backups"] is True
        assert results[0]["suggested_path"] == os.path.join(
            os.path.normpath(od_path), "Backups", "NoteHelper"
        )

    def test_detect_deduplicates(self, tmp_path, monkeypatch):
        """Same path from multiple sources should only appear once."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(od_path)
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.setenv("OneDrive", od_path)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        results = detect_onedrive_paths()
        paths = [r["path"] for r in results]
        assert paths.count(os.path.normpath(od_path)) == 1

    def test_detect_folder_scan(self, tmp_path, monkeypatch):
        """Detects OneDrive* folders under USERPROFILE."""
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        # Create a OneDrive folder in the temp "home"
        od_path = tmp_path / "OneDrive - Microsoft"
        od_path.mkdir()

        results = detect_onedrive_paths()
        paths = [r["path"] for r in results]
        assert os.path.normpath(str(od_path)) in paths

    def test_detect_returns_empty_when_nothing_found(self, tmp_path, monkeypatch):
        """Returns empty list if no OneDrive found."""
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # tmp_path has no OneDrive* folders

        results = detect_onedrive_paths()
        assert results == []

    def test_auto_detected_path_with_single_match(self, tmp_path, monkeypatch):
        """get_auto_detected_backup_path returns the path when one has backups."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(os.path.join(od_path, "Backups", "NoteHelper"))
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        result = get_auto_detected_backup_path()
        assert result == os.path.join(os.path.normpath(od_path), "Backups", "NoteHelper")

    def test_auto_detected_path_with_no_backups_folder(self, tmp_path, monkeypatch):
        """Returns None when OneDrive exists but no Backups/NoteHelper subfolder."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(od_path)
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        result = get_auto_detected_backup_path()
        assert result is None

    def test_auto_detected_path_none_when_nothing(self, tmp_path, monkeypatch):
        """Returns None when no OneDrive found at all."""
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = get_auto_detected_backup_path()
        assert result is None


class TestDetectOneDriveRoute:
    """Test the detect-onedrive API route."""

    @pytest.fixture(autouse=True)
    def _mock_registry(self, monkeypatch):
        """Prevent real Windows registry from leaking into tests."""
        def _no_reg(*args, **kwargs):
            raise OSError("mocked - no registry")
        monkeypatch.setattr("app.services.backup.winreg.OpenKey", _no_reg)

    def test_detect_route_returns_candidates(self, client, tmp_path, monkeypatch):
        """The route returns detected candidates."""
        od_path = str(tmp_path / "OneDrive - Microsoft")
        os.makedirs(os.path.join(od_path, "Backups", "NoteHelper"))
        monkeypatch.setenv("OneDriveCommercial", od_path)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nonexistent"))

        resp = client.get("/api/backup/detect-onedrive")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "candidates" in data
        assert "auto_path" in data
        assert len(data["candidates"]) >= 1
        assert data["auto_path"] is not None

    def test_detect_route_no_onedrive(self, client, tmp_path, monkeypatch):
        """The route returns empty when no OneDrive found."""
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        resp = client.get("/api/backup/detect-onedrive")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["candidates"] == []
        assert data["auto_path"] is None


# =========================================================================
# find_backup_folder
# =========================================================================


class TestFindBackupFolder:
    """Tests for the find_backup_folder helper."""

    def test_finds_folder_from_config(self, app, backup_dir, backup_config):
        """Should find the call_logs subfolder from an enabled config."""
        with app.app_context():
            # Create the call_logs subfolder
            call_logs = os.path.join(backup_dir, "call_logs")
            os.makedirs(call_logs, exist_ok=True)

            result = find_backup_folder()
            assert result == call_logs

    def test_returns_none_when_no_folder(self, app, backup_dir, monkeypatch):
        """Should return None when no call_logs folder exists anywhere."""
        with app.app_context():
            # Config pointing at dir with no call_logs subfolder
            monkeypatch.setattr(
                "app.services.backup._load_db_backup_config",
                lambda: {"enabled": True, "backup_dir": backup_dir},
            )

            # Stub out all OneDrive detection so real machine paths don't leak in
            monkeypatch.setattr(
                "app.services.backup.detect_onedrive_paths", lambda: []
            )
            monkeypatch.setattr(
                "app.services.backup.get_auto_detected_backup_path", lambda: None
            )

            result = find_backup_folder()
            assert result is None


# =========================================================================
# restore_all_from_folder
# =========================================================================


class TestRestoreAllFromFolder:
    """Tests for the bulk restore from backup folder."""

    def _write_backup_json(self, folder: str, seller: str, tpid: int,
                           customer_name: str, call_logs: list) -> str:
        """Helper to write a backup JSON file in the proper folder structure."""
        seller_dir = os.path.join(folder, "call_logs", seller)
        os.makedirs(seller_dir, exist_ok=True)
        filepath = os.path.join(seller_dir, f"{tpid}.json")
        data = {
            "_notehelper_backup": True,
            "_version": 2,
            "_exported_at": datetime.now(timezone.utc).isoformat(),
            "customer": {
                "name": customer_name,
                "tpid": tpid,
                "seller_name": seller,
            },
            "call_logs": call_logs,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return filepath

    def test_restore_all_creates_call_logs(self, app, backup_dir):
        """Should restore call logs from multiple files across sellers."""
        with app.app_context():
            user = User.query.first()
            seller = Seller(name="TestSeller", alias="ts", seller_type="Growth")
            db.session.add(seller)
            db.session.flush()

            c1 = Customer(name="Customer A", tpid=50001, seller_id=seller.id)
            c2 = Customer(name="Customer B", tpid=50002, seller_id=seller.id)
            db.session.add_all([c1, c2])
            db.session.commit()

            # Write backup files
            call_logs_dir = os.path.join(backup_dir, "call_logs")
            self._write_backup_json(backup_dir, "TestSeller", 50001, "Customer A", [
                {"call_date": "2025-01-10T10:00:00+00:00", "content": "Log 1", "topics": [], "partners": []},
                {"call_date": "2025-01-11T10:00:00+00:00", "content": "Log 2", "topics": [], "partners": []},
            ])
            self._write_backup_json(backup_dir, "TestSeller", 50002, "Customer B", [
                {"call_date": "2025-02-01T14:00:00+00:00", "content": "Log 3", "topics": [], "partners": []},
            ])

        # Use test_request_context so g.user is set by before_request
        with app.test_request_context():
            app.preprocess_request()
            result = restore_all_from_folder(call_logs_dir)
            assert result["success"] is True
            assert result["files_processed"] == 2
            assert result["total_logs_created"] == 3
            assert result["files_failed"] == 0
            assert "Customer A" in result["customers_restored"]
            assert "Customer B" in result["customers_restored"]

    def test_restore_all_skips_existing_logs(self, app, backup_dir):
        """Should skip call logs that already exist (by date dedup)."""
        with app.app_context():
            user = User.query.first()
            seller = Seller(name="DedupSeller", alias="ds", seller_type="Growth")
            db.session.add(seller)
            db.session.flush()

            customer = Customer(name="Dedup Customer", tpid=60001,
                                seller_id=seller.id)
            db.session.add(customer)
            db.session.flush()

            # Pre-existing call log
            existing = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="Already here",
            )
            db.session.add(existing)
            db.session.commit()

            call_logs_dir = os.path.join(backup_dir, "call_logs")
            self._write_backup_json(backup_dir, "DedupSeller", 60001, "Dedup Customer", [
                {"call_date": "2025-03-01T10:00:00+00:00", "content": "Already here", "topics": [], "partners": []},
                {"call_date": "2025-03-02T10:00:00+00:00", "content": "New one", "topics": [], "partners": []},
            ])

        with app.test_request_context():
            app.preprocess_request()
            result = restore_all_from_folder(call_logs_dir)
            assert result["success"] is True
            assert result["total_logs_created"] == 1
            assert result["total_logs_skipped"] == 1

    def test_restore_all_handles_bad_json(self, app, backup_dir):
        """Should count corrupt files as failures and continue."""
        with app.app_context():
            user = User.query.first()
            seller = Seller(name="BadJsonSeller", alias="bj", seller_type="Growth")
            db.session.add(seller)
            db.session.flush()
            customer = Customer(name="Good Customer", tpid=70001,
                                seller_id=seller.id)
            db.session.add(customer)
            db.session.commit()

            # Write one good and one bad file
            call_logs_dir = os.path.join(backup_dir, "call_logs")
            self._write_backup_json(backup_dir, "BadJsonSeller", 70001, "Good Customer", [
                {"call_date": "2025-04-01T10:00:00+00:00", "content": "Works", "topics": [], "partners": []},
            ])
            bad_dir = os.path.join(call_logs_dir, "BadJsonSeller")
            with open(os.path.join(bad_dir, "corrupt.json"), "w") as f:
                f.write("{not valid json!!!")

        with app.test_request_context():
            app.preprocess_request()
            result = restore_all_from_folder(call_logs_dir)
            assert result["success"] is True
            assert result["files_processed"] == 1
            assert result["files_failed"] == 1
            assert len(result["errors"]) == 1

    def test_restore_all_returns_error_when_no_folder(self, app):
        """Should return error when folder doesn't exist."""
        with app.app_context():
            result = restore_all_from_folder("/nonexistent/path")
            assert result["success"] is False
            assert "Could not find" in result["error"]

    def test_restore_all_empty_folder(self, app, backup_dir):
        """Should succeed with zero files when folder is empty."""
        with app.app_context():
            call_logs_dir = os.path.join(backup_dir, "call_logs")
            os.makedirs(call_logs_dir, exist_ok=True)

            result = restore_all_from_folder(call_logs_dir)
            assert result["success"] is True
            assert result["files_processed"] == 0
            assert result["total_logs_created"] == 0


# =========================================================================
# Restore-All Route
# =========================================================================


class TestRestoreAllRoute:
    """Tests for POST /api/backup/restore-all."""

    def test_restore_all_blocked_without_account_sync(self, client, app):
        """Should return 400 when accounts haven't been synced."""
        with app.app_context():
            from app.models import SyncStatus
            # Make sure no accounts sync status exists
            SyncStatus.query.filter_by(sync_type="accounts").delete()
            db.session.commit()

        resp = client.post("/api/backup/restore-all")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "accounts" in data["error"].lower()

    def test_restore_all_allowed_after_account_sync(self, client, app, backup_dir, monkeypatch):
        """Should proceed when accounts sync is complete."""
        with app.app_context():
            from app.models import SyncStatus
            SyncStatus.mark_started("accounts")
            SyncStatus.mark_completed("accounts", success=True, items_synced=10)
            db.session.commit()

        # Point find_backup_folder at a dir with no files
        call_logs_dir = os.path.join(backup_dir, "call_logs")
        os.makedirs(call_logs_dir, exist_ok=True)
        monkeypatch.setattr(
            "app.services.backup.find_backup_folder",
            lambda: call_logs_dir,
        )

        resp = client.post("/api/backup/restore-all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["files_processed"] == 0


# =========================================================================
# Customer notes backup integration
# =========================================================================

class TestCustomerNotesBackupIntegration:
    """Verify that customer notes changes trigger backup and restore correctly."""

    def test_update_notes_triggers_backup(self, client, app, backup_dir, sample_data, monkeypatch):
        """Saving customer notes should write a backup JSON file."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": True, "backup_dir": backup_dir},
        )

        resp = client.post(
            f"/customer/{sample_data['customer1_id']}/notes",
            data={"notes": "Key engagement notes for Acme"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200

        # customer1 has tpid=1001 and seller=Alice Smith
        filepath = os.path.join(backup_dir, "call_logs", "Alice Smith", "1001.json")
        assert os.path.isfile(filepath), f"Expected backup at {filepath}"

        with open(filepath, "r") as f:
            backup = json.load(f)
        assert backup["customer"]["notes"] == "Key engagement notes for Acme"

    def test_update_notes_backup_disabled(self, client, app, backup_dir, sample_data, monkeypatch):
        """No backup file when backup is disabled."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": False, "backup_dir": ""},
        )
        monkeypatch.setattr(
            "app.services.backup.get_auto_detected_backup_path", lambda: None,
        )

        resp = client.post(
            f"/customer/{sample_data['customer1_id']}/notes",
            data={"notes": "Should not trigger backup"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200

        cl_dir = os.path.join(backup_dir, "call_logs")
        assert not os.path.exists(cl_dir)

    def test_edit_customer_triggers_backup(self, client, app, backup_dir, sample_data, monkeypatch):
        """Editing a customer via the form should write a backup JSON file."""
        monkeypatch.setattr(
            "app.services.backup._load_db_backup_config",
            lambda: {"enabled": True, "backup_dir": backup_dir},
        )

        resp = client.post(
            f"/customer/{sample_data['customer1_id']}/edit",
            data={
                "name": "Acme Corp Updated",
                "tpid": "1001",
                "seller_id": str(sample_data["seller1_id"]),
                "territory_id": str(sample_data["territory1_id"]),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        filepath = os.path.join(backup_dir, "call_logs", "Alice Smith", "1001.json")
        assert os.path.isfile(filepath), f"Expected backup at {filepath}"

        with open(filepath, "r") as f:
            backup = json.load(f)
        assert backup["customer"]["name"] == "Acme Corp Updated"

    def test_restore_includes_notes(self, app):
        """Restoring a backup with notes should set the customer notes field."""
        with app.app_context():
            from flask import g
            user = User.query.first()
            g.user = user

            customer = Customer(name="Notes Restore Corp", tpid=77777)
            db.session.add(customer)
            db.session.commit()
            assert customer.notes is None

            backup_data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {
                    "name": "Notes Restore Corp",
                    "tpid": 77777,
                    "notes": "Restored engagement summary",
                },
                "call_logs": [],
            }

            result = restore_from_backup(backup_data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.notes == "Restored engagement summary"

            # Cleanup
            db.session.delete(customer)
            db.session.commit()

    def test_restore_does_not_overwrite_existing_notes(self, app):
        """Restoring should not overwrite notes if the customer already has them."""
        with app.app_context():
            from flask import g
            user = User.query.first()
            g.user = user

            customer = Customer(
                name="Existing Notes Corp", tpid=88888,
                notes="My existing notes",
            )
            db.session.add(customer)
            db.session.commit()

            backup_data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {
                    "name": "Existing Notes Corp",
                    "tpid": 88888,
                    "notes": "Backup notes that should NOT overwrite",
                },
                "call_logs": [],
            }

            result = restore_from_backup(backup_data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.notes == "My existing notes"

            # Cleanup
            db.session.delete(customer)
            db.session.commit()

    def test_restore_without_notes_field(self, app):
        """Restoring a backup that has no notes key should leave notes as None."""
        with app.app_context():
            from flask import g
            user = User.query.first()
            g.user = user

            customer = Customer(name="No Notes Corp", tpid=66666)
            db.session.add(customer)
            db.session.commit()

            backup_data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {
                    "name": "No Notes Corp",
                    "tpid": 66666,
                },
                "call_logs": [],
            }

            result = restore_from_backup(backup_data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.notes is None

            # Cleanup
            db.session.delete(customer)
            db.session.commit()
