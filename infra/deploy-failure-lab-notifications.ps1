<#
.SYNOPSIS
    Deploy the approved isolated completion-mail endpoint and enable lab notifications.
.DESCRIPTION
    Reuses only the verified sender domain and configured support recipient.
    Does not redeploy the existing email app, ACS endpoint, domain, APIM or gateway.
#>
param([switch]$WhatIf)
$ErrorActionPreference = 'Stop'

function Invoke-LabAzure {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $output = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Lab notification Azure operation failed.' }
    return $output
}

$account = Invoke-LabAzure @('account','show','-o','json') | ConvertFrom-Json
if ($account.id -ne '97aba503-25b6-46a2-8ed2-a8afc3bfbd23' -or
    $account.tenantId -ne '96d12531-723e-46c1-842b-0480739c7419') {
    throw 'Wrong hosting Azure context. This script never switches accounts.'
}
$group = 'NoteHelper_Resources'
$app = 'func-salesbuddy-failure-lab'
$mailBase = "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$group/providers/Microsoft.Communication/communicationServices"
$domain = Invoke-LabAzure @('rest','--method','get','--url',
    "$mailBase/salesbuddy-diagnostics-mail?api-version=2023-03-31",
    '--query','properties.linkedDomains[0]','-o','tsv')
if (-not $domain -or $domain -notmatch '/Microsoft.Communication/emailServices/.+/domains/AzureManagedDomain$') {
    throw 'Existing verified sender domain was not found.'
}
$principal = Invoke-LabAzure @('functionapp','identity','show','-g',$group,'-n',$app,
    '--query','principalId','-o','tsv')
if (-not $principal) { throw 'Lab managed identity was not found.' }
$arguments = @('deployment','group')
if ($WhatIf) { $arguments += 'what-if' } else { $arguments += 'create' }
$arguments += @('-g',$group,'--name','salesbuddy-failure-lab-notifications',
    '--template-file',(Join-Path $PSScriptRoot 'failure-lab-notifications.bicep'),
    '--parameters',"verifiedDomainId=$domain","labPrincipalId=$principal")
if (-not $WhatIf) { $arguments += @('--output','none') }
Invoke-LabAzure -Arguments $arguments
if ($WhatIf) { return }

$endpoint = Invoke-LabAzure @('rest','--method','get','--url',
    "$mailBase/salesbuddy-failure-lab-mail?api-version=2023-03-31",
    '--query','properties.hostName','-o','tsv')
$source = Invoke-LabAzure @('functionapp','config','appsettings','list','-g',$group,
    '-n','func-salesbuddy-diag-private','-o','json') | ConvertFrom-Json
$sender = ($source | Where-Object name -eq 'ACS_SENDER').value
$recipient = ($source | Where-Object name -eq 'SUPPORT_EMAIL').value
if (-not $endpoint -or -not $sender -or -not $recipient) {
    throw 'Isolated email settings could not be prepared.'
}
$null = Invoke-LabAzure @('functionapp','config','appsettings','set','-g',$group,'-n',$app,
    '--settings','COMPLETION_EMAIL_ENABLED=false',"COMPLETION_EMAIL_ENDPOINT=https://$endpoint",
    "COMPLETION_EMAIL_SENDER=$sender","COMPLETION_EMAIL_RECIPIENT=$recipient",'--output','none')
& (Join-Path $PSScriptRoot 'deploy-failure-investigation.ps1') -SkipInfrastructure
$null = Invoke-LabAzure @('functionapp','config','appsettings','set','-g',$group,'-n',$app,
    '--settings','COMPLETION_EMAIL_ENABLED=true','--output','none')
Write-Host 'Completion notifications enabled for the one approved lab task.'
Write-Host 'Existing email application, sender endpoint, domain and recipient settings are unchanged.'
