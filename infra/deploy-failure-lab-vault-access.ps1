<#
.SYNOPSIS
    Preview or deploy four private-vault networking resources and the disabled lab uploader.
.DESCRIPTION
    Does not redeploy existing email, APIM, vault, VNet or Function infrastructure.
#>
param([switch]$WhatIf)
$ErrorActionPreference = 'Stop'

$accountJson = & az account show -o json
if ($LASTEXITCODE -ne 0) { throw 'Hosting Azure context is unavailable.' }
$account = $accountJson | ConvertFrom-Json
if ($account.tenantId -ne '96d12531-723e-46c1-842b-0480739c7419' -or
    $account.id -ne '97aba503-25b6-46a2-8ed2-a8afc3bfbd23') {
    throw 'Wrong hosting Azure context. This script never changes accounts.'
}
$arguments = @('deployment', 'group')
if ($WhatIf) { $arguments += 'what-if' } else { $arguments += 'create' }
$arguments += @('-g', 'NoteHelper_Resources', '--name', 'salesbuddy-failure-vault-access',
    '--template-file', (Join-Path $PSScriptRoot 'failure-lab-vault-private-access.bicep'),
    '--parameters', 'location=centralus')
if (-not $WhatIf) { $arguments += @('--output', 'none') }
& az @arguments
if ($LASTEXITCODE -ne 0) { throw 'Private vault networking operation failed.' }
if ($WhatIf) { return }

& az functionapp config appsettings set -g NoteHelper_Resources `
    -n func-salesbuddy-failure-lab --settings 'TOKEN_UPLOAD_EXPIRES_AT=' --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not disable the temporary token upload window.' }
& (Join-Path $PSScriptRoot 'deploy-failure-investigation.ps1') -SkipInfrastructure
