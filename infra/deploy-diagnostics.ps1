<#
.SYNOPSIS
    Deploy the private-storage Flex diagnostics service and its dedicated APIM API.
.DESCRIPTION
    Run only after Azure provisioning approval. Uses the existing CLI context, managed
    identities and OneDeploy remote build. Never enables public storage or reads keys.
    The original classic Consumption Function and unrelated APIM APIs are untouched.
#>
param(
    [string]$ResourceGroup = 'NoteHelper_Resources',
    [string]$SupportEmail = 'alex.blaine@microsoft.com',
    [string]$FunctionName = 'func-salesbuddy-diag-private',
    [string]$ApplicationId = 'e59cf4a7-76d2-4014-b10b-2cbcd2b41297',
    [switch]$SkipInfrastructure
)
$ErrorActionPreference = 'Stop'

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure operation failed: $($Arguments[0]) $($Arguments[1])" }
    return $output
}

$account = Invoke-Azure @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.tenantId -ne '96d12531-723e-46c1-842b-0480739c7419' -or
    $account.id -ne '97aba503-25b6-46a2-8ed2-a8afc3bfbd23') {
    throw 'Wrong current Azure context. This script never changes accounts or profiles.'
}
$apim = Invoke-Azure @('apim', 'show', '-g', $ResourceGroup, '-n',
    'apim-notehelper', '-o', 'json') | ConvertFrom-Json
if (-not $apim.identity.principalId) { throw 'APIM requires a system managed identity.' }
if (-not $SkipInfrastructure) {
    Invoke-Azure @('deployment', 'group', 'create', '-g', $ResourceGroup,
        '--name', 'salesbuddy-diagnostics-private',
        '--template-file', (Join-Path $PSScriptRoot 'diagnostics.bicep'),
        '--parameters', "apimPrincipalId=$($apim.identity.principalId)",
        "applicationId=$ApplicationId", "supportEmail=$SupportEmail",
        "functionName=$FunctionName", '--output', 'none')
}
$base = "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroup"
$site = Invoke-Azure @('rest', '--method', 'get', '--url',
    "$base/providers/Microsoft.Web/sites/$($FunctionName)?api-version=2024-04-01",
    '-o', 'json') | ConvertFrom-Json
$backend = "https://$($site.properties.defaultHostName)"
# OneDeploy streams the source to the integrated Flex build service. That service,
# not the operator's public workstation, writes the private deployment container.
$stage = Join-Path $PSScriptRoot ('.diagnostics-deploy-' + [guid]::NewGuid().ToString('N'))
New-Item $stage -ItemType Directory | Out-Null
try {
    $package = Join-Path $stage 'source'
    New-Item $package -ItemType Directory | Out-Null
    foreach ($name in @('function_app.py', 'service.py', 'requirements.txt', 'host.json')) {
        Copy-Item (Join-Path $PSScriptRoot "diagnostics\$name") $package
    }
    $archive = Join-Path $stage 'diagnostics.zip'
    Compress-Archive -Path (Join-Path $package '*') -DestinationPath $archive
    Invoke-Azure @('functionapp', 'deployment', 'source', 'config-zip',
        '-g', $ResourceGroup, '-n', $FunctionName, '--src', $archive,
        '--build-remote', 'true', '--timeout', '1200', '--output', 'none')
    $indexed = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        $functions = @(Invoke-Azure @('functionapp', 'function', 'list', '-g',
            $ResourceGroup, '-n', $FunctionName, '--query', '[].name', '-o', 'json') |
            ConvertFrom-Json)
        if ($functions.Count -eq 5) { $indexed = $true; break }
        Start-Sleep -Seconds 10
    }
    if (-not $indexed) { throw 'Expected five indexed functions before switching APIM.' }
    $apiRoot = "$base/providers/Microsoft.ApiManagement/service/apim-notehelper/apis/salesbuddy-diagnostics"
    $file = Join-Path $stage 'request.json'
    $api = @{ properties = @{
        displayName = 'Sales Buddy diagnostics'; path = 'diagnostics'; protocols = @('https')
        subscriptionRequired = $false; serviceUrl = $backend
    }} | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($file, $api)
    Invoke-Azure @('rest', '--method', 'put', '--url', "$($apiRoot)?api-version=2022-08-01",
        '--body', "@$file", '--output', 'none')
    foreach ($operation in @('reports', 'followups')) {
        $body = @{ properties = @{
            displayName = $operation; method = 'POST'; urlTemplate = "/$operation"
            templateParameters = @(); responses = @()
        }} | ConvertTo-Json -Depth 10
        [IO.File]::WriteAllText($file, $body)
        Invoke-Azure @('rest', '--method', 'put', '--url',
            "$apiRoot/operations/$($operation)?api-version=2022-08-01",
            '--body', "@$file", '--output', 'none')
    }
    $policy = (Get-Content (Join-Path $PSScriptRoot 'diagnostics-apim-policy.xml') -Raw).
        Replace('https://func-salesbuddy-diag-private.azurewebsites.net', $backend).
        Replace('api://e59cf4a7-76d2-4014-b10b-2cbcd2b41297', "api://$ApplicationId")
    [IO.File]::WriteAllText($file, (@{ properties = @{
        format = 'rawxml'; value = $policy
    }} | ConvertTo-Json -Depth 5))
    Invoke-Azure @('rest', '--method', 'put', '--url',
        "$apiRoot/policies/policy?api-version=2022-08-01",
        '--body', "@$file", '--output-file', (Join-Path $stage 'policy-response.xml'),
        '--output', 'none')
    foreach ($url in @("$backend/api/reports", "$($apim.gatewayUrl)/diagnostics/reports")) {
        $response = Invoke-WebRequest $url -Method Post -ContentType 'application/json' `
            -Body '{}' -SkipHttpErrorCheck
        if ([int]$response.StatusCode -ne 401) {
            throw "Anonymous rejection failed for $url ($($response.StatusCode))."
        }
    }
    Write-Host "Deployed $FunctionName with five indexed functions. Backend and APIM reject anonymous requests."
    Write-Host 'Verify a corporate submission and durable email delivery before enabling clients.'
} finally {
    # Delete only this invocation's generated staging directory under infra.
    Remove-Item -LiteralPath $stage -Recurse -Force
}
