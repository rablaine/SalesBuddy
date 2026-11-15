# Isolated failure investigations

This is a separate Azure Functions deployment, not part of the Sales Buddy app,
AI gateway, or diagnostics email deployment. Nothing imports it from those systems.
It uses the email app's existing `succeeded` report write as a trigger, checked
once per minute. No email-app code or deployment changes are required.

## Production promotion

The production profile targets only `rablaine/SalesBuddy` and reuses the existing
isolated Function, private storage, private vault and completion-mail endpoint.
It does not connect to, upload or change a user's Sales Buddy database.
The original diagnostics/email deployment, app, updater, APIM and gateway stay untouched.

- Admit only new update-failure records whose original support email has succeeded.
  `PRODUCTION_STARTED_AT` is an aware UTC activation cutoff, frozen in independent
  production control state. Historical reports are not automatically investigated.
- Admit one investigation per normalized, trusted sign-in email + exact target
  Git commit, across retries, restarts and multiple computers. Another user or
  target commit may create another investigation; production has no one-per-day
  ceiling. Bounded round-robin pages limit work per timer tick without dropping reports.
- A private persisted HMAC salt keeps reporter emails out of investigation state,
  task prompts, PRs and status responses. Deduplication receipts remain in the
  independent storage, not in diagnostics.
- Missing identity or target commit requires manual review. The original failure
  email is still delivered. Invalid technical metadata is also recorded for manual review.
- The public repository receives only the existing intake's strictly allowlisted
  technical report fields: phases, exception types/categories, known file/line
  references, counters, known table names and commit IDs. Raw attachments, free-text
  logs, reporter contact and customer information remain in the original support
  email/storage, not in GitHub.
- Persist user/version admission and `submission_uncertain` before GitHub POST.
  Never automatically resubmit an uncertain or rejected task for that user/version.
  Such cases require operator reconciliation, not a new report to bypass the gate.
- Copilot proposes a review PR and the isolated sender emails its verified link.
  There is no automatic merge, deployment, workflow modification or database access.

After explicit approval, promote **code only, disabled**:

```powershell
.\infra\deploy-failure-investigation.ps1 -Production
```

This packages `production.py` and a read-only copy of
`infra/diagnostics/service.py` as `diagnostics_contract.py`, reusing the actual
intake validator without redeploying or changing the email service.
The production PAT reference is the separate `github-salesbuddy-token` secret.
The old synthetic secret, upload receipt and experiment records are not overwritten.
No additional Azure resources are created.

Create a fine-grained PAT scoped only to `rablaine/SalesBuddy`, with Agent tasks
read/write and Pull requests read permission. Upload it locally, with hidden input:

```powershell
.\infra\failure_investigation\set-lab-token.ps1 -Production
```

The legacy filename reflects the reused infrastructure. `-Production` binds both
readiness and POST to the real repository, a separate create-only secret and
`token-uploads/production.json`. An old client cannot upload to the production
profile without its explicit matching header. The upload window defaults closed;
the temporary vault write grant and expiry are cleaned up in `finally`.
Do not paste tokens into chat, command arguments, logs or files. The uploader
does not enable investigations, rotate an existing secret or reset uncertain writes.

Before activation, verify the new Key Vault reference resolves and the upload
window and temporary role are closed. With explicit approval, set
`PRODUCTION_STARTED_AT` to the current UTC instant, `INVESTIGATION_MODE=submit`
and `INVESTIGATION_ENABLED=true`. Preserve that cutoff on later re-enablement.
Use `-SkipInfrastructure` for subsequent code-only deployments that should
preserve production settings; `-Production` deliberately pauses admission.
The protected status endpoint lists the last completed timer tick and bounded
safe investigation and report receipt metadata. More pages are indicated by
`*_has_more`; operator reconciliation can
inspect the corresponding private production state without resetting admissions.

## Synthetic profile (original experiment)

- Synthetic-only. Configure one report ID and the exact approved hash of its
  report and attachments. The fixture is `synthetic_payload.json`.
- Only a dedicated test repository is permitted; a repository named `SalesBuddy`
  is rejected. Production integration requires a separately reviewed change.
- Disabled by default. Preview mode stores a prompt without obtaining a GitHub
  credential or making any GitHub request.
- Diagnostics storage is read-only. All locks, previews, task status and the
  submission budget live in a separate private storage account.
- One worker, one global lease, at most one task submission per UTC day, and at
  most one submission per configured report. Disable the Function app setting
  `INVESTIGATION_ENABLED` to stop all new submissions and polling.
- GitHub does not document a task-creation idempotency key. Persist uncertain
  submission state before POST and never automatically retry it. Check GitHub
  manually before resolving an uncertain submission.
- Only report content and approved synthetic attachments enter the prompt.
  Reporter contact, identity and email delivery data never enter GitHub.
- No merge, deployment, customer-system integration, or GitHub workflow creation
  is implemented. Disabling the experiment does not cancel an already running
  Copilot task: stop that separately through GitHub if necessary.

The exact fixture hash is a synthetic approval boundary, not a general-purpose
redaction guarantee. Production never reuses the synthetic raw-attachment prompt.
Existing support attachments may contain customer or confidential data even after
credential redaction and are intentionally not forwarded to GitHub.

## Deploy, disabled

After approval, run from the repository root:

```powershell
.\infra\deploy-failure-investigation.ps1 -WhatIf
.\infra\deploy-failure-investigation.ps1
```

The script uses the existing hosting Azure CLI context. It never switches
profiles. The template creates `func-salesbuddy-failure-lab`, its Flex plan,
`stsalesbuddyfixlab`, three private endpoints, DNS records, and identity grants.
It also creates the dedicated RBAC-protected Key Vault `kvsalesbuddyfixlab`, with
secret-management access for the hosting operator and secret-read access for only
the new Function identity. The vault starts empty and public access is disabled;
app settings contain only a Key Vault reference, not a token.
The hosting management group's `KeyVault_PublicNetwork_Modify` governance policy
forces public access off, including when an update requests it on. Do not attempt
to override or exempt this policy. The separately approved private-vault network
addition described below lets the existing lab Function resolve and access the
vault. Preview mode does not require the token.

It reuses the diagnostics VNet and existing storage DNS zones without redeploying
them. The only grant on existing diagnostics is container-scoped Blob Data Reader
for the new app. Existing email, APIM and gateway resources are not redeployed.

Azure private endpoints and DNS incur costs even while the app is idle. Function,
storage and Copilot usage are separately billed. Deployment verifies three indexed
functions, anonymous rejection on `/api/status`, and authenticated disabled status.
Function keys must never be printed, committed or passed to Copilot.

## Minimal temporary token uploader

The larger undeployed networking proposal was discarded. Run
`.\infra\deploy-failure-lab-vault-access.ps1 -WhatIf`, then deploy only after approval.
It adds four resources: a private vault endpoint, a new vault DNS zone, its VNet
link and the endpoint's DNS zone group. The existing vault, VNet, subnets and
email infrastructure are not redeployed. It redeploys code only to the isolated
lab Function app with the upload window disabled, preserving experiment settings.

From a local PowerShell 7 terminal with the existing hosting Azure CLI context:

```powershell
.\infra\failure_investigation\set-lab-token.ps1
```

The script prompts with hidden input. It never writes the token to a file,
process argument, output or this chat. It retrieves only the upload Function's
key using authenticated Azure management access, creates a temporary lab-vault
Secrets Officer grant for the lab identity, and opens a fifteen-minute window.
Readiness creates a non-secret upload marker to prove private state-storage write
access and confirms the private vault is reachable and empty before HTTPS POST
sends the token to the lab Function with a stable request ID.
The Function writes
only `github-test-token` through its private endpoint. No GitHub call is made.
During a configuration restart, readiness HTTP 410 (old closed configuration)
and HTTP 503 are retried within the bounded window. Other rejection codes stop
the script. HTTP 503 and lost POST responses are retried with the same request ID
within the expiry window. A stale HTTP 410 after successful readiness is retried
with that same request ID, only within the original expiry and 24-attempt bound;
the uploader never extends or reopens the window to recover a POST.
A completed request returns confirmation without writing
again; an uncertain write returns HTTP 409 and stops for operator reconciliation.
Definitive Azure 403 secret-write denials retain a retryable permission-pending
state because Azure rejected the write. Other write failures remain uncertain.

In `finally`, the script clears the window and deletes only the role assignment
it created. If the terminal is forcibly terminated, expiry still closes the
endpoint; the operator must remove any surviving temporary grant separately.
An upload marker in independent state prevents repeated secret writes or uncertain uploads.
The endpoint refuses an existing secret rather than overwriting it. Ambiguous
secret-write failures require operator review, not another secret write.

The endpoint requires Function-key authentication, HTTPS and an active bounded
expiry. It defaults to HTTP 410 even with a valid key. It logs neither request
bodies nor credential exception messages. Do not enable body tracing, transcripts,
debug logging or session recording during secret entry. RBAC and Function-key
access must be restricted to authorized operators. This is an explicitly
authorized temporary credential-ingestion endpoint, not a public vault endpoint.
Keep the reusable provisioning workflow after successful upload, but verify that
the upload window is closed and the temporary write grant was revoked. The endpoint
must not accept credentials outside an explicitly opened window. Uploading the
token does not enable Copilot task creation. Production provisioning and rotation
will need a separately approved credential, vault and repository scope; this
uploader remains fixed to the lab and must not be repointed at production.

## Verify the synthetic flow

1. Create a private dedicated test repository and upload the contents of
   `synthetic_repository`. Its regression test intentionally fails.
2. Submit `synthetic_payload.json` through the existing diagnostics APIM route,
   using the dedicated development corporate Azure CLI profile. This sends one
   clearly synthetic support email; never upload directly to diagnostics storage
   or alter a real report. Confirm the email and attachments arrive.
3. Set `INVESTIGATION_ENABLED=true`, leaving `INVESTIGATION_MODE=preview`.
   After email delivery succeeds, `/api/status` should report `preview`.
   The prompt contains only the approved fake evidence, not the sign-in email.
   Flex configuration changes can cause a several-minute cold start with transient
   timeout/503 responses. Wait for authenticated status before declaring the handoff verified.
4. Before submit mode, create a fine-grained user token scoped **only** to the
   private test repository, with Agent tasks read/write permission and any
   additional permissions required by the installed Copilot plan. The task API
   is preview; verify the plan supports task creation. Installation tokens
   are not supported. Never reuse a broadly scoped production GitHub credential.
5. Use the local temporary uploader above to store that credential as
   `github-test-token` in the dedicated `kvsalesbuddyfixlab` vault. The Function
   identity's permanent secret-read grant and `GITHUB_TOKEN` Key Vault reference
   are configured by the deployment; write access is temporary.
   Do not put the token into source, command output, fixtures or a prompt.
6. After explicit approval, set `INVESTIGATION_MODE=submit`. The recorded preview
   advances to one Copilot task with `create_pull_request=true`, based on `main`.
7. Check protected `/api/status` for `task_id`, task state and `task_url`.
   The official GitHub task page provides the review handoff to the generated PR
   and branch without additional PR-read permission. Task artifact IDs are database
   IDs, not PR numbers; never turn them into `/pull/` links. Completed older records
   refresh once to replace guessed PR links without submitting another task.
   Poll failures do not create another task. GitHub PR notifications provide the
   default review handoff; the optional isolated completion sender is described below.
8. Review the PR and its scoped test results. Do not merge it automatically.

## Completion email

After approval, preview and deploy the isolated completion-email addition:

```powershell
.\infra\deploy-failure-lab-notifications.ps1 -WhatIf
.\infra\deploy-failure-lab-notifications.ps1
```

This adds one ACS communication endpoint and its lab-only sender role assignment.
It references the existing verified domain without updating it. The broad sender
role is scoped only to the new endpoint, never the existing diagnostics sender.
No new email domain, storage, network, original-email deployment or credentials
are required. The configured recipient and sender are copied from the existing
email app without printing them.

`COMPLETION_EMAIL_ENABLED` defaults to false. When explicitly enabled, the existing
worker sends one HTML completion notification, with a plain-text fallback, for a successfully
completed task. Short link labels keep Outlook Safe Links destinations out of the
visible HTML text. The template deliberately leaves background, text and link
colors to the mail client's native theme. Explicit dark fills were inverted to
light in desktop Outlook even with dark mode on; color-scheme hints did not
prevent this. Typography and spacing stay inline, with no app CSS or downloaded
fonts. The message retains its direct
PR link, task-review link and report ID.
It includes no logs,
attachments or reporter contact. Direct PR lookup requires Pull requests read
permission on the repository-scoped token. Missing permission is visible in the
protected status endpoint as notification stage `pr_lookup` and HTTP 403; no email
is sent with a guessed or substituted link.

Notification state lives inside the existing independent investigation record.
Delivery follows the diagnostics sender's stable ACS operation-ID protocol:
persist before POST, reconcile uncertain sends by polling the same operation,
bound retries and confirm `Succeeded` before reporting delivery. Repeated ticks,
restarts and completed-task polls never create another Copilot task or a duplicate
accepted send. Routing changes during delivery require operator review.
An unknown operation after an uncertain send, including HTTP 404, is never
automatically resent. Only explicit authentication rejections allow a send retry.
Disable notifications with `COMPLETION_EMAIL_ENABLED=false`; previously accepted
ACS operations can still finish. The protected status exposes notification
status, stage, safe HTTP code and PR link, never email addresses or credentials.

For an explicitly approved theme-inheriting preview, temporarily set
`COMPLETION_EMAIL_PREVIEW=theme-inherited-v2` on the lab Function, then restore it to
`false` after verification. The worker uses the already-confirmed original PR
link and a separate create-only preview receipt and operation ID. It sends one
`[Preview]` message for this template version without resetting the original
notification or creating another Copilot task. Status is exposed as `email_preview`.
Re-enabling the same preview version cannot resend its completed receipt.
The earlier dark-template receipt is retained unchanged; this version uses a
different receipt and operation ID. Confirm the delivered message in Outlook
with dark mode still on, not just the HTML source or ACS delivery status.

The status endpoint exposes only IDs, status, HTTP rejection code and a fixed
test-repository task review link. It does not expose packet contents or credentials.
It also exposes the token upload marker's status and safe HTTP error code when
available, so an operator can distinguish an unstarted, completed or uncertain upload.
Protect operator keys and restrict access to the experiment storage.

## Validation

Activate the repository venv and run only the related offline tests:

```powershell
& .\venv\Scripts\Activate.ps1
pytest tests/test_failure_investigation.py tests/test_diagnostics_service.py tests/test_diagnostics_infrastructure.py
```

Do not run the intentionally failing lab repository test as part of Sales Buddy's
regression suite. That failure is the input to the separate Copilot experiment.

GitHub API contract:
<https://docs.github.com/en/rest/agent-tasks/agent-tasks>
