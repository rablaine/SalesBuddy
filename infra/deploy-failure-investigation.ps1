<#
.SYNOPSIS
    Deploy only the isolated synthetic investigation app, disabled and without GitHub secrets.
.DESCRIPTION
    Requires explicit Azure approval. Uses the existing hosting CLI context without
    switching accounts. Does not deploy diagnostics, modify APIM, or enable task submission.
#>
param(
    [string]$ResourceGroup = 'NoteHelper_Resources',
    [string]$FunctionName = 'func-salesbuddy-failure-lab',
    [string]$TestRepository = 'rablaine/salesbuddy-failure-lab',
    [switch]$WhatIf,
    [switch]$SkipInfrastructure,
    [switch]$Production
)
$ErrorActionPreference = 'Stop'
if ($Production) { $SkipInfrastructure = $true }
if ($Production -and ($FunctionName -ne 'func-salesbuddy-failure-lab' -or
                     $ResourceGroup -ne 'NoteHelper_Resources')) {
    throw 'Production promotion reuses only the approved existing isolated Function.'
}

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure operation failed: $($Arguments[0]) $($Arguments[1])" }
    return $output
}

if ($FunctionName -notmatch '^func-salesbuddy-failure-lab(?:-[a-z0-9-]+)?$' -or
    $TestRepository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
    $TestRepository.Split('/')[1] -ieq 'SalesBuddy') {
    throw 'Only an isolated lab Function app and dedicated test repository are allowed.'
}
$account = Invoke-Azure @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.tenantId -ne '96d12531-723e-46c1-842b-0480739c7419' -or
    $account.id -ne '97aba503-25b6-46a2-8ed2-a8afc3bfbd23') {
    throw 'Wrong hosting Azure context. This script never changes accounts or profiles.'
}
$repoRoot = Split-Path $PSScriptRoot
& (Join-Path $repoRoot 'venv\Scripts\Activate.ps1')
$fixture = Join-Path $PSScriptRoot 'failure_investigation\synthetic_payload.json'
$approvalJson = & python -c @'
import hashlib, json, sys
p = json.load(open(sys.argv[1], encoding='utf-8'))
c = {'report': p['report'], 'attachments': p['attachments']}
b = json.dumps(c, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
print(json.dumps({'id': p['report_id'], 'hash': hashlib.sha256(b).hexdigest()}))
'@ $fixture
if ($LASTEXITCODE -ne 0) { throw 'Cannot compute the synthetic fixture approval.' }
$approval = $approvalJson | ConvertFrom-Json
$parameters = @(
    "functionName=$FunctionName", "testRepository=$TestRepository",
    "syntheticReportId=$($approval.id)", "syntheticContentHash=$($approval.hash)"
)
if (-not $SkipInfrastructure -or ($WhatIf -and -not $Production)) {
    $operatorId = Invoke-Azure @('ad', 'signed-in-user', 'show', '--query', 'id', '-o', 'tsv')
    if (-not $operatorId) { throw 'A signed-in hosting user is required to manage the test vault.' }
    $parameters += "operatorPrincipalId=$operatorId"
}
$template = Join-Path $PSScriptRoot 'failure-investigation.bicep'
if ($WhatIf) {
    if ($Production) {
        Write-Host 'Production reuses the existing isolated resources: code only, disabled.'
        Write-Host 'The runtime will reference the separate github-salesbuddy-token secret.'
        return
    }
    Invoke-Azure -Arguments (@('deployment', 'group', 'what-if', '-g', $ResourceGroup,
        '--template-file', $template, '--parameters') + $parameters)
    return
}
if (-not $SkipInfrastructure) {
    Invoke-Azure -Arguments (@('deployment', 'group', 'create', '-g', $ResourceGroup,
        '--name', 'salesbuddy-failure-lab', '--template-file', $template,
        '--parameters') + $parameters + @('--output', 'none'))
}
if ($Production) {
    $settingsFile = Join-Path ([IO.Path]::GetTempPath()) `
        ('salesbuddy-production-settings-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
        @{
            INVESTIGATION_ENABLED = 'false'
            INVESTIGATION_PROFILE = 'production'
            INVESTIGATION_MODE = 'preview'
            COMPLETION_EMAIL_PREVIEW = 'false'
            TOKEN_UPLOAD_EXPIRES_AT = ''
            TOKEN_UPLOAD_PROFILE = 'production'
            GITHUB_TOKEN = '@Microsoft.KeyVault(SecretUri=https://kvsalesbuddyfixlab.vault.azure.net/secrets/github-salesbuddy-token/)'
        } | ConvertTo-Json | Set-Content -LiteralPath $settingsFile -Encoding utf8
        Invoke-Azure @('functionapp', 'config', 'appsettings', 'set',
            '-g', $ResourceGroup, '-n', $FunctionName, '--settings', "@$settingsFile",
            '--output', 'none') | Out-Null
    } finally {
        Remove-Item -LiteralPath $settingsFile -ErrorAction Stop
    }
}
$stage = Join-Path $PSScriptRoot ('.failure-lab-deploy-' + [guid]::NewGuid().ToString('N'))
New-Item $stage -ItemType Directory | Out-Null
try {
    $package = Join-Path $stage 'source'
    New-Item $package -ItemType Directory | Out-Null
    foreach ($name in @('function_app.py', 'service.py', 'production.py',
                       'requirements.txt', 'host.json')) {
        Copy-Item (Join-Path $PSScriptRoot "failure_investigation\$name") $package
    }
    Copy-Item (Join-Path $PSScriptRoot 'diagnostics\service.py') `
        (Join-Path $package 'diagnostics_contract.py')
    $archive = Join-Path $stage 'failure-lab.zip'
    Compress-Archive -Path (Join-Path $package '*') -DestinationPath $archive
    Invoke-Azure @('functionapp', 'deployment', 'source', 'config-zip',
        '-g', $ResourceGroup, '-n', $FunctionName, '--src', $archive,
        '--build-remote', 'true', '--timeout', '1200', '--output', 'none')
    $indexed = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        $names = @(Invoke-Azure @('functionapp', 'function', 'list', '-g',
            $ResourceGroup, '-n', $FunctionName, '--query', '[].name', '-o', 'json') |
            ConvertFrom-Json)
        if ($names.Count -eq 3 -and
            ($names | Where-Object { $_ -match '/investigate$' }) -and
            ($names | Where-Object { $_ -match '/status$' }) -and
            ($names | Where-Object { $_ -match '/upload_token$' })) {
            $indexed = $true
            break
        }
        Start-Sleep -Seconds 10
    }
    if (-not $indexed) { throw 'Expected the timer, status and protected token uploader.' }
    $site = Invoke-Azure @('rest', '--method', 'get', '--url',
        "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$($FunctionName)?api-version=2024-04-01",
        '-o', 'json') | ConvertFrom-Json
    $url = "https://$($site.properties.defaultHostName)/api/status"
    $response = Invoke-WebRequest $url -SkipHttpErrorCheck
    if ([int]$response.StatusCode -ne 401) {
        throw "Anonymous status access was not rejected ($($response.StatusCode))."
    }
    $keys = Invoke-Azure @('functionapp', 'function', 'keys', 'list', '-g',
        $ResourceGroup, '-n', $FunctionName, '--function-name', 'status', '-o', 'json') |
        ConvertFrom-Json
    $statusVerified = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        try {
            $statusResponse = Invoke-WebRequest $url -Headers @{ 'x-functions-key' = $keys.default } `
                -SkipHttpErrorCheck -TimeoutSec 15
        } catch {
            Write-Warning 'Lab status startup request unavailable; retrying the read-only check.'
            Start-Sleep -Seconds 10
            continue
        }
        if ([int]$statusResponse.StatusCode -eq 200) {
            $response = $statusResponse.Content | ConvertFrom-Json
            $statusVerified = $true
            break
        }
        if ([int]$statusResponse.StatusCode -notin @(500, 503)) {
            throw "Protected lab status returned HTTP $($statusResponse.StatusCode)."
        }
        Start-Sleep -Seconds 10
    }
    if (-not $statusVerified) { throw 'Protected lab status did not become ready after deployment.' }
    if (-not $SkipInfrastructure -and $response.enabled -ne $false) {
        throw 'Experiment must remain disabled after initial deployment.'
    }
    if ($Production -and $response.enabled -ne $false) {
        throw 'Production must remain disabled until token provisioning and explicit activation.'
    }
    Write-Host "Verified ${FunctionName}: three indexed functions and protected status."
    if ($SkipInfrastructure) {
        Write-Host 'Existing experiment settings were preserved.'
    } else {
        Write-Host 'The initial experiment remains disabled.'
    }
    Write-Host 'Existing email app and APIM are unchanged.'
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
