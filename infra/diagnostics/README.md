# Diagnostics intake

Independent Azure Python Functions v2 deployment. No product imports, database,
account keys, user email payloads or raw logs. Deploy this directory as the app
root. The approved deployment is provisioned with `infra\diagnostics.bicep` and
`infra\deploy-diagnostics.ps1`.

## Trust boundary (required infrastructure)

* APIM validates the Microsoft corporate JWT signature, issuer, audience,
  expiration and corporate tenant. Apply a 16 KiB request limit. APIM Consumption
  cannot use `rate-limit-by-key` or `quota-by-key`; a shared subscription-level
  `rate-limit` can provide a coarse additional guard.
  Strip inbound `X-SalesBuddy-Contact` and all caller-supplied EasyAuth identity
  headers. Never forward the corporate caller identity to Functions.
* For **followups only**, APIM selects the first usable email address from the
  validated `preferred_username`, `upn`, `email`, then `unique_name` claims and
  overwrites `X-SalesBuddy-Contact`. Skip missing or non-email claims rather
  than selecting an unusable preferred claim. A legitimate guest-domain email
  is permitted: validated corporate-tenant authentication establishes trust,
  not the email domain. Never use unvalidated headers or body values. If no
  claim is usable, return the same clear `422` contact error documented below
  (or forward no contact header so the Function returns it). Never inject the
  contact header for reports.
* On **both intake POSTs**, strip/overwrite `X-SalesBuddy-Rate-Key` with the
  lowercase 64-character SHA256 hex digest of the validated caller's `oid`.
  Reject JWTs without that claim. Never forward the raw oid or accept a caller's
  chosen rate key. This header is separate from the APIM managed identity
  principal used for backend authentication. Health needs no rate header.
* APIM authenticates to the Function app with its managed identity and the
  backend app's configured audience. EasyAuth **must require authentication**,
  reject unauthenticated calls with 401, validate that audience/issuer, and
  restrict the allowed principal to APIM. APIM Consumption needs public Function
  ingress: EasyAuth requires the APIM identity on every backend path, including
  health, so a direct hostname is not an authentication bypass. Storage ingress
  remains private through Blob, Queue and Table private endpoints.
* Functions HTTP auth is anonymous because EasyAuth enforces the outer boundary.
  Every HTTP handler additionally checks the platform-generated
  `X-MS-CLIENT-PRINCIPAL-ID` against `APIM_PRINCIPAL_ID`. A raw header alone is not
  authentication. Never run these endpoints publicly without EasyAuth.
* Disable request/response body, JWT, custom identity-header and contact logging
  in APIM, EasyAuth, Functions and tracing. Anonymous report storage contains no
  caller identity, IP, email or tokens. Infrastructure access logs must be
  assessed separately; do not describe them as anonymous report records.

## Settings and provisioning

| Setting | Value |
| --- | --- |
| `APIM_PRINCIPAL_ID` | `1f06d5c5-5480-4cdc-90cb-3e02cbe6fbbc` |
| `REPORT_STORAGE_URL` | `https://stsalesbuddydiag.blob.core.windows.net/` |
| `REPORT_CONTAINER` | Precreated private container, default `diagnostics` |
| `DIAGNOSTICS_QUEUE_NAME` | Precreated queue name, set explicitly to `diagnostics` |
| `REPORT_QUEUE__queueServiceUri` | `https://stsalesbuddydiag.queue.core.windows.net` |
| `REPORT_QUEUE__credential` | `managedidentity` in Azure |
| `AzureWebJobsStorage__accountName` | `stsalesbuddydiag` |
| `AzureWebJobsStorage__credential` | `managedidentity` in Azure |
| `ACS_ENDPOINT` | `https://<resource>.communication.azure.com` |
| `ACS_SENDER` | ACS verified sender address |
| `SUPPORT_EMAIL` | `alex.blaine@microsoft.com`, fixed operator-controlled recipient |

Use a system-assigned Function identity. Grant Storage Blob Data Contributor on
the diagnostics container and Storage Queue Data Contributor on the queue.
Grant the identity the ACS resource-scoped Contributor role for this deployment
(the hosting subscription has no narrow email-sender built-in role). Provision the host identity
storage permissions required by Azure Functions (Blob Data Owner and Queue Data
Contributor, plus account Contributor when required by the selected hosting plan).
Host and application storage use separate containers in the same private account.
For a user-assigned identity,
also configure the host/binding identity settings and `AZURE_CLIENT_ID`.
ACS must have a linked verified email domain/sender. The API token scope is
`https://communication.azure.com/.default`. Never configure caller-chosen
recipients. No email credentials are required.

Target app: `func-salesbuddy-diag-private` (Flex Consumption, Python 3.11), in the hosting tenant
beginning `96d12531`. Configure EasyAuth's **full** hosting tenant ID in
infrastructure, not an abbreviated ID. Backend app audience:
`api://e59cf4a7-76d2-4014-b10b-2cbcd2b41297`. The caller corporate JWT tenant
validated at APIM is separate from this backend hosting tenant. `ACS_SENDER`
must be the actual verified `DoNotReply@<azure-managed-domain>` address.

### Private deployment

Run `.\infra\deploy-diagnostics.ps1` from the repository root after provisioning
approval. It checks the current tenant and subscription without changing CLI
profiles, applies the infrastructure, streams source with Flex OneDeploy remote
build, verifies all five functions are indexed, then updates only the diagnostics
APIM API and checks anonymous POST rejection. `-SkipInfrastructure` redeploys code
and policy to an already provisioned environment. No storage keys, SAS URLs,
public storage access, deployment VM or container runner are used. Source staging
is confined to a per-run directory under `infra` and removed in `finally`.

The dedicated VNet is `vnet-salesbuddy-diagnostics` (`10.92.0.0/16`), with
`functions` delegated to `Microsoft.App/environments` and a separate
`private-endpoints` subnet. Three private DNS zones are linked to that VNet.
The Flex plan is `plan-salesbuddy-diag-flex`, without always-ready instances.
Storage is `stsalesbuddydiag`, shared-key authentication and public networking
disabled; `diagnostics` and `deployment` are private containers, and
`diagnostics` is the precreated dispatch queue. Managed identity authorizes host,
deployment and application data access. The system identity also has Contributor
scoped only to `salesbuddy-diagnostics-mail` for ACS sending.

The original `func-salesbuddy-diagnostics` and `CentralUSLinuxDynamicPlan` are
preserved, but no longer serve diagnostics traffic. Existing grants to that old
identity are preserved pending explicit cleanup approval. The unrelated test
VNet and VM are untouched. There are no continuously running deployment helpers
or NAT gateways. Private endpoint hourly/data charges and private DNS charges
apply even while Flex scales to zero; Function executions, storage operations
and ACS email are usage-billed. Review regional prices before estimating costs.

Deployment verification is not delivery verification: before enabling clients,
send one sanitized synthetic report through APIM using a corporate credential
and verify the exact 202 receipt and eventual ACS delivery. Do not grant a second
caller direct backend access just to perform this test.

On October 22, 2026, synthetic report
`f1e48619f81f4e97ac541ba6c41f549c` and its separate followup both returned
the exact HTTP 202 `accepted` receipt through corporate-authenticated APIM.
A 16,385-byte request was rejected against the 16,384-byte limit. Authorized
ACS operation-status GETs returned HTTP 200 with `Succeeded` for report operation
`f1e48619-f81f-4e97-ac54-1ba6c41f549c` and followup operation
`d8596437-5ae4-5dfb-8f14-156c892f5911`. This proves successful ACS dispatch,
not inbox delivery; delivery metrics were not yet populated. Offline policy
regressions are in `tests\test_diagnostics_infrastructure.py`.

## Contract

* `POST /api/reports`: `{schema_version:1, report_id:"<32 lowercase hex>",
  report:{...}}`. The nested report is the sanitized
  `scripts/upgrade.py::failure_report` shape, re-allowlisted independently.
  Commit hashes are optional 40-64 lowercase hex. At least one failure section
  is required. Unknown fields, exception messages, paths, arbitrary table names,
  filenames and phases are rejected, not copied into storage. Failure sections
  may contain at most two distinct `web`/`worker` children with bounded attempts
  and boolean readiness. Numeric types are strict (booleans are not integers).
* `POST /api/followups`: `{schema_version:1, report_id:"<32 lowercase hex>"}`.
  Requires the trusted email contact header. This is explicit opt-in; an
  initial report never creates a contact event. Followups may arrive before the
  anonymous report: accept them independently and link by report ID, without
  copying identity into the report. Support follows up manually in Teams/email.
* `GET /api/health`: generic `ok`, still behind the same authentication.
* The exact successful receipt for either POST, including an identical retry,
  is HTTP `202`, `Content-Type: application/json`, with the JSON object
  `{"report_id":"<same 32 lowercase hex ID>","status":"accepted"}`. There are
  exactly these two fields. JSON whitespace/key order is not a client contract.
  The desktop must validate the HTTP code, exact report ID and `accepted`
  status before deleting its local queued item.
* `202` means **durably accepted**, never email delivered. Identical retry is
  idempotent. Different report content or contact for the same ID returns `409`.
  Invalid input returns `400`, bad identity `403`, missing/unusable trusted
  contact returns `422`, storage
  failure `503`. New events over the daily caller limit return `429` with
  `Retry-After` seconds and `{"error":"submission_limit_reached"}`. Missing or
  malformed rate-key headers return `400`. Client may retry a failed/uncertain
  receipt with the same ID.
  The exact `422` JSON object is
  `{"error":"contact_email_unavailable","message":"No usable email contact claim is available."}`.
  This never creates a contact blob or sends an email. Report intake does not
  require a contact claim.

Before enabling desktop submissions, set all table settings, precreate the private
container and queue, verify identity role assignments and the mandatory APIM/
EasyAuth trust boundary, and confirm the Functions host indexes `reports`,
`followups`, `health`, `dispatch` and `repair`. `REPORT_QUEUE` is the **connection
prefix**, not the queue-name setting; set both
`REPORT_QUEUE__queueServiceUri` and `REPORT_QUEUE__credential=managedidentity`.
Set `DIAGNOSTICS_QUEUE_NAME=diagnostics` explicitly for trigger name resolution.
The queue client uses that same URI and name with `DefaultAzureCredential`.
`AzureWebJobsStorage__accountName` configures host storage, not this queue binding.
The health response confirms handler availability only; it does not probe storage
or ACS. Offline tests verify binding metadata and client wiring, not Azure RBAC,
network reachability, actual queue-trigger delivery or email delivery.

## Durability, retries and retention

Intake admits at most **20 new events per caller per UTC day**, shared between
anonymous reports and followups. Exact stored duplicates return `202` even when
the daily budget is exhausted or its counter is currently leased; conflicting
content still returns `409`. A well-formed trusted rate header is always required
for intake, including retries. Health bypasses rate checks.

Create-only counters at `rate/<UTC-date>/<SHA256-oid>.json` contain only a count
and creation timestamp, never raw oid, email, report IDs or report content.
An exclusive 60-second blob lease serializes same-caller admission; contention
returns `429` with `Retry-After: 1`. Recheck dedupe after acquiring the lease,
reserve quota durably before creating the event, then release. Counter creation
races use create-if-absent. Storage errors fail closed with `503`. There is no
cross-blob transaction: a crash/storage failure between reservation and event
creation can conservatively consume a slot without creating an event. A race
submitting identical new content under different caller keys can also consume
an extra reservation; durable event dedupe remains unchanged. No raw or hashed
caller identifier is stored inside anonymous reports or contact events. Hashes
are pseudonymous rate-control identifiers, not a claim of irreversible anonymity.
The minute cleanup expires counters after 30 days; include `rate/` in the same
independent creation-age lifecycle policy as the other prefixes.

Create-only blobs `reports/<id>.json` store anonymous failure events;
`contacts/<id>.json` store separate opt-in contact events, linked by report ID.
The canonical content hash rejects conflicting retries. Write the blob before
queueing its name; only durable creation earns a receipt. A queue trigger sends
immediately when possible, including `recovery: incomplete`. The desktop must
submit the initial failure as soon as failure is detected, **before restore**;
this service never waits for restore or opt-in before dispatching its email.
The ID's first anonymous payload is immutable, so recovery changes must not
overwrite a previously accepted report. A **periodic queue repair scan every 1 minute** scans
both blob prefixes even when enqueue failed. That scan also polls pending ACS
operations and cleans expired events; it does not depend on queue retention.
Queue poison messages cannot strand their durable events.

Every send/poll/delete acquires a 60-second blob lease. HTTP calls have bounded
connect/read timeouts, never follow redirects, and do not persist ACS response
bodies. A persisted pre-send state plus stable ACS `Operation-Id` (report UUID
for failures, UUIDv5 for followups) reconciles ambiguous sends through polling
before retrying a missing operation. Status becomes `succeeded` **only** when
ACS returns `Succeeded`; ACS acceptance or 202 is not delivery confirmation.
This confirms ACS completion, not inbox receipt/read. Terminal ACS failures
remain failed; transport errors back off up to one hour with a 12-error limit.
Missing operations permit at most 12 submission attempts. Polling stops after
1440 polls. No automatic new operation ID is minted.

Exactly-once email delivery cannot be guaranteed across external-service
deduplication windows, process crashes, lease expiry or delayed ACS operation
visibility. Reusing the same operation ID and leasing minimizes duplicate sends;
do not claim strict at-most-once or exactly-once semantics. Monitor failed event
counts/queue poison metrics without logging contents. Large deployments should
partition scans or replace the full-container scan before it exceeds the
10-minute host budget.

Both event types expire 30 days after creation, regardless of state. The minute
scan deletes expired blobs under lease. Also provision **independent storage
lifecycle delete rules** on both prefixes with a 30-day creation-age limit as a
backstop when the app is down. Ensure versioning, soft-delete, snapshots,
backups, diagnostic exports and lifecycle policies do not retain identity beyond
the approved retention period. Restrict contact-prefix access to support staff.
Delivery state overwrites must not extend retention (use creation age, not
last-modified age). ACS/mailbox retention must be configured separately; emails
also contain the opted-in address. Dedupe ends when the blob is deleted; clients
must not replay expired reports. No deployment is performed by this package.
