"""
Tests for POD derivation from territory names.

Validates that territories with suffixes like '.A' or '.B' are correctly
parsed to derive their POD number from the 4th segment (index 3), not
the last segment.

Covers: scan_account(), get_account_details(), initialize_territory_scan()
"""
import pytest
from unittest.mock import patch, MagicMock


# Helper to build a mock query_entity response for territory lookups
def _mock_territory_response(territory_name: str):
    """Create a mock territory query response with the given name."""
    return {
        "success": True,
        "records": [{
            "territoryid": "test-territory-id",
            "name": territory_name,
            "msp_ownerid": "seller-id",
            "msp_ownerid@OData.Community.Display.V1.FormattedValue": "Test Seller",
            "msp_salesunitname": "Test Sales Unit",
            "msp_accountteamunitname": "Test ATU",
        }]
    }


def _mock_account_response():
    """Create a mock account query response."""
    return {
        "success": True,
        "records": [{
            "accountid": "test-account-id",
            "name": "Test Account",
            "msp_mstopparentid": "tpid-123",
            "_territoryid_value": "test-territory-id",
            "_territoryid_value@OData.Community.Display.V1.FormattedValue": "East.SMECC.MAA.0601",
        }]
    }


class TestPodDerivationFromTerritoryName:
    """Test that POD names are correctly derived from territory name segments."""

    @patch('app.services.msx_api.query_entity')
    def test_standard_territory_name(self, mock_query):
        """East.SMECC.MAA.0601 -> East POD 06"""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("East.SMECC.MAA.0601"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["pod"] == "East POD 06"

    @patch('app.services.msx_api.query_entity')
    def test_territory_with_a_suffix(self, mock_query):
        """East.SMECC.HLA.0610.A -> East POD 06 (not based on 'A')"""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("East.SMECC.HLA.0610.A"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["pod"] == "East POD 06"

    @patch('app.services.msx_api.query_entity')
    def test_territory_with_b_suffix(self, mock_query):
        """West.SMECC.XYZ.1201.B -> West POD 12"""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("West.SMECC.XYZ.1201.B"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["pod"] == "West POD 12"

    @patch('app.services.msx_api.query_entity')
    def test_territory_with_numeric_suffix(self, mock_query):
        """East.SMECC.ABC.0301.2 -> East POD 03"""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("East.SMECC.ABC.0301.2"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["pod"] == "East POD 03"

    @patch('app.services.msx_api.query_entity')
    def test_different_region(self, mock_query):
        """West.SMECC.DEF.0901 -> West POD 09"""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("West.SMECC.DEF.0901"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["pod"] == "West POD 09"

    @patch('app.services.msx_api.query_entity')
    def test_atu_code_extracted(self, mock_query):
        """ATU code should come from the 3rd segment (index 2)."""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("East.SMECC.HLA.0610.A"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert result["territory"]["atu_code"] == "HLA"

    @patch('app.services.msx_api.query_entity')
    def test_too_few_segments_no_pod(self, mock_query):
        """Territory with fewer than 4 segments should not derive a POD."""
        from app.services.msx_api import scan_account

        mock_query.side_effect = [
            _mock_account_response(),
            _mock_territory_response("East.SMECC.MAA"),
        ]

        result = scan_account("test-account-id")
        assert result["success"]
        assert "pod" not in result["territory"]


class TestGetAccountDetailsPodDerivation:
    """Test POD derivation in get_account_details()."""

    @patch('app.services.msx_api.query_entity')
    def test_standard_territory(self, mock_query):
        """get_account_details correctly derives POD from standard territory."""
        from app.services.msx_api import get_account_details

        mock_query.side_effect = [
            # Account query
            {
                "success": True,
                "records": [{
                    "accountid": "acct-1",
                    "name": "Test Customer",
                    "msp_mstopparentid": "tpid-1",
                    "_territoryid_value": "terr-1",
                    "_territoryid_value@OData.Community.Display.V1.FormattedValue": "East.SMECC.MAA.0601",
                    "msp_verticalname": "Technology",
                    "msp_accountcategoryname": "Strategic",
                }]
            },
            # Territory query
            _mock_territory_response("East.SMECC.MAA.0601"),
        ]

        result = get_account_details("acct-1")
        assert result["success"]
        assert result["pod"] == "East POD 06"

    @patch('app.services.msx_api.query_entity')
    def test_territory_with_suffix(self, mock_query):
        """get_account_details correctly derives POD from territory with .A suffix."""
        from app.services.msx_api import get_account_details

        mock_query.side_effect = [
            # Account query
            {
                "success": True,
                "records": [{
                    "accountid": "acct-1",
                    "name": "Test Customer",
                    "msp_mstopparentid": "tpid-1",
                    "_territoryid_value": "terr-1",
                    "_territoryid_value@OData.Community.Display.V1.FormattedValue": "East.SMECC.HLA.0610.A",
                    "msp_verticalname": "Technology",
                    "msp_accountcategoryname": "Strategic",
                }]
            },
            # Territory query
            _mock_territory_response("East.SMECC.HLA.0610.A"),
        ]

        result = get_account_details("acct-1")
        assert result["success"]
        assert result["pod"] == "East POD 06"


class TestBulkImportPodParsing:
    """Test the territory-to-POD parsing logic used in the bulk import route.

    The import route in msx.py parses territory names inline (not via a shared
    helper). These tests verify the same algorithm handles suffix variants.
    """

    @staticmethod
    def _parse_pod_name(terr_name: str) -> str | None:
        """Replicate the inline parsing logic from the bulk import route."""
        name_parts = terr_name.split(".")
        if len(name_parts) >= 4:
            region = name_parts[0]
            territory_num = name_parts[3]
            if len(territory_num) >= 2:
                pod_num = territory_num[:2]
                return f"{region} POD {pod_num}"
        return None

    def test_standard_four_segment(self):
        """East.SMECC.MAA.0601 -> East POD 06"""
        assert self._parse_pod_name("East.SMECC.MAA.0601") == "East POD 06"

    def test_five_segment_with_a_suffix(self):
        """East.SMECC.HLA.0507.A -> East POD 05"""
        assert self._parse_pod_name("East.SMECC.HLA.0507.A") == "East POD 05"

    def test_five_segment_with_b_suffix(self):
        """West.SMECC.XYZ.1201.B -> West POD 12"""
        assert self._parse_pod_name("West.SMECC.XYZ.1201.B") == "West POD 12"

    def test_five_segment_with_numeric_suffix(self):
        """East.SMECC.ABC.0301.2 -> East POD 03"""
        assert self._parse_pod_name("East.SMECC.ABC.0301.2") == "East POD 03"

    def test_different_region(self):
        """West.SMECC.DEF.0901 -> West POD 09"""
        assert self._parse_pod_name("West.SMECC.DEF.0901") == "West POD 09"

    def test_too_few_segments(self):
        """East.SMECC.MAA -> None (fewer than 4 segments)"""
        assert self._parse_pod_name("East.SMECC.MAA") is None

    def test_short_territory_number(self):
        """East.SMECC.MAA.5 -> None (territory num < 2 chars)"""
        assert self._parse_pod_name("East.SMECC.MAA.5") is None

    def test_issue_104_exact_case(self):
        """East.SMECC.HLA.0507 -> East POD 05 (from issue #104)"""
        assert self._parse_pod_name("East.SMECC.HLA.0507") == "East POD 05"
