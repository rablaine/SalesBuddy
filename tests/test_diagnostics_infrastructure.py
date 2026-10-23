"""Offline contract checks for the diagnostics APIM trust boundary."""

from pathlib import Path
from xml.etree import ElementTree

import pytest


@pytest.fixture
def inbound_policy() -> ElementTree.Element:
    """Read the checked-in policy without contacting Azure."""
    path = Path(__file__).resolve().parents[1] / "infra" / "diagnostics-apim-policy.xml"
    inbound = ElementTree.parse(path).getroot().find("inbound")
    assert inbound is not None
    return inbound


def test_schema_less_json_is_not_rejected(inbound_policy: ElementTree.Element) -> None:
    """Prevent the live 400 caused by unspecified application/json schemas."""
    content = inbound_policy.find("validate-content")
    assert content is not None
    assert content.get("unspecified-content-type-action") == "ignore"
    header = inbound_policy.find("check-header[@name='Content-Type']")
    assert header is not None
    assert header.get("failed-check-httpcode") == "415"
    assert header.get("ignore-case") == "true"
    assert [value.text for value in header.findall("value")] == ["application/json"]


def test_body_size_stays_enforced(inbound_policy: ElementTree.Element) -> None:
    """Ignoring absent schemas must not disable the 16 KiB size gate."""
    content = inbound_policy.find("validate-content")
    assert content is not None
    assert content.get("max-size") == "16384"
    assert content.get("size-exceeded-action") == "prevent"


def test_corporate_jwt_stays_required(inbound_policy: ElementTree.Element) -> None:
    """Keep signed, expiring corporate tokens ahead of backend authentication."""
    jwt = inbound_policy.find("validate-jwt")
    assert jwt is not None
    for name, value in {
        "header-name": "Authorization",
        "require-scheme": "Bearer",
        "require-expiration-time": "true",
        "require-signed-tokens": "true",
        "failed-validation-httpcode": "401",
        "output-token-variable-name": "diagnosticCaller",
    }.items():
        assert jwt.get(name) == value
    tenant = "72f988bf-86f1-41af-91ab-2d7cd011db47"
    discovery = jwt.find("openid-config")
    assert discovery is not None
    assert discovery.get("url") == (
        f"https://login.microsoftonline.com/{tenant}/.well-known/openid-configuration"
    )
    assert [node.text for node in jwt.findall("audiences/audience")] == [
        "https://management.azure.com"
    ]
    assert {node.text for node in jwt.findall("issuers/issuer")} == {
        f"https://sts.windows.net/{tenant}/",
        f"https://login.microsoftonline.com/{tenant}/v2.0",
    }
    claim = jwt.find("required-claims/claim[@name='tid']")
    assert claim is not None
    assert claim.get("match") == "all"
    assert [node.text for node in claim.findall("value")] == [tenant]


def test_backend_uses_managed_identity(inbound_policy: ElementTree.Element) -> None:
    """Require APIM's backend identity rather than forwarding caller credentials."""
    identity = inbound_policy.find("authentication-managed-identity")
    assert identity is not None
    assert identity.get("resource") == "api://e59cf4a7-76d2-4014-b10b-2cbcd2b41297"
    assert identity.get("ignore-error", "false") == "false"
    backend = inbound_policy.find("set-backend-service")
    assert backend is not None
    assert backend.get("base-url") == (
        "https://func-salesbuddy-diag-private.azurewebsites.net"
    )
    policies = list(inbound_policy)
    assert policies.index(inbound_policy.find("validate-jwt")) < policies.index(identity)
    assert policies.index(inbound_policy.find("validate-content")) < policies.index(identity)


@pytest.mark.parametrize("name", [
    "X-MS-CLIENT-PRINCIPAL-ID",
    "X-MS-CLIENT-PRINCIPAL",
    "X-MS-CLIENT-PRINCIPAL-NAME",
    "X-MS-CLIENT-PRINCIPAL-IDP",
    "X-SalesBuddy-Contact",
])
def test_untrusted_identity_headers_are_stripped(
    inbound_policy: ElementTree.Element, name: str,
) -> None:
    """Never trust client-supplied platform identity or contact headers."""
    header = inbound_policy.find(f"set-header[@name='{name}']")
    assert header is not None
    assert header.get("exists-action") == "delete"
