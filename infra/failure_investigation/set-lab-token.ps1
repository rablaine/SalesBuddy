<#
.SYNOPSIS
    Upload a repository-scoped token through the temporary authenticated isolated Function.
.DESCRIPTION
    Requires the existing hosting Azure CLI sign-in. Secret input is never put into
    process arguments, files, or output. Do not run under transcription or debug logging.
#>
param([switch]$Production)
$ErrorActionPreference = 'Stop'
$profile = if ($Production) { 'production' } else { 'synthetic' }
$repository = if ($Production) { 'rablaine/SalesBuddy' } else { 'rablaine/salesbuddy-failure-lab' }

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure lab operation failed: $($Arguments[0])" }
    return $output
}
$account = Invoke-Azure @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.tenantId -ne '96d12531-723e-46c1-842b-0480739c7419' -or
    $account.id -ne '97aba503-25b6-46a2-8ed2-a8afc3bfbd23') {
    throw 'Use the existing hosting Azure CLI context; this script never switches accounts.'
}
$token = Read-Host "Paste the token scoped ONLY to $repository" -AsSecureString
$pointer = [IntPtr]::Zero
$key = $null
$body = $null
$uploadRoleId = $null
$requestId = [guid]::NewGuid().ToString('N')
try {
    if ($token.Length -eq 0) { throw 'No token supplied. Nothing was uploaded.' }
    $principalId = Invoke-Azure @('functionapp', 'identity', 'show', '-g',
        'NoteHelper_Resources', '-n', 'func-salesbuddy-failure-lab',
        '--query', 'principalId', '-o', 'tsv')
    $vaultId = "/subscriptions/$($account.id)/resourceGroups/NoteHelper_Resources/providers/Microsoft.KeyVault/vaults/kvsalesbuddyfixlab"
    $assignment = Invoke-Azure @('role', 'assignment', 'create',
        '--assignee-object-id', $principalId, '--assignee-principal-type', 'ServicePrincipal',
        '--role', 'Key Vault Secrets Officer', '--scope', $vaultId,
        '--name', ([guid]::NewGuid().ToString()), '-o', 'json') | ConvertFrom-Json
    $uploadRoleId = $assignment.id
    $expires = [DateTimeOffset]::UtcNow.AddMinutes(15).ToString('o')
    Invoke-Azure @('functionapp', 'config', 'appsettings', 'set', '-g',
        'NoteHelper_Resources', '-n', 'func-salesbuddy-failure-lab',
        '--settings', "TOKEN_UPLOAD_EXPIRES_AT=$expires", "TOKEN_UPLOAD_PROFILE=$profile",
        '--output', 'none') | Out-Null
    $keys = Invoke-Azure @('functionapp', 'function', 'keys', 'list', '-g',
        'NoteHelper_Resources', '-n', 'func-salesbuddy-failure-lab',
        '--function-name', 'upload_token', '-o', 'json') | ConvertFrom-Json
    $key = $keys.default
    $keys = $null
    if (-not $key) { throw 'The temporary upload function key is unavailable.' }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($token)
    $body = @{ token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) } |
        ConvertTo-Json -Compress
    $url = 'https://func-salesbuddy-failure-lab.azurewebsites.net/api/token-upload'
    Write-Host 'Waiting for private vault readiness, then uploading over HTTPS through the lab Function.'
    $ready = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        if ([DateTimeOffset]::UtcNow -ge ([DateTimeOffset]::Parse($expires).AddSeconds(-90))) {
            throw 'The upload window expired before vault readiness. No upload attempted.'
        }
        try {
            $response = Invoke-WebRequest $url -Method Get `
                -Headers @{ 'x-functions-key' = $key } -TimeoutSec 15 `
                -SkipHttpErrorCheck -MaximumRedirection 0 -Verbose:$false -Debug:$false
        } catch {
            Write-Warning 'The readiness request failed; retrying before any credential upload.'
            Start-Sleep -Seconds 10
            continue
        }
        if ([int]$response.StatusCode -eq 200) {
            $readiness = $response.Content | ConvertFrom-Json
            if ($readiness.status -ne 'ready' -or $readiness.secret_present -ne $false) {
                throw 'The scoped vault secret already exists or is not ready. No upload attempted.'
            }
            if ($readiness.profile -ne $profile -or $readiness.repository -ne $repository) {
                throw 'The upload scope does not match the requested repository. No upload attempted.'
            }
            $ready = $true
            break
        }
        if ([int]$response.StatusCode -notin @(410, 503)) {
            throw "Upload refused with HTTP $($response.StatusCode). No response content is printed."
        }
        Start-Sleep -Seconds 10
    }
    if (-not $ready) { throw 'Private vault readiness was not confirmed. No upload attempted.' }
    $result = $null
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        if ([DateTimeOffset]::UtcNow -ge ([DateTimeOffset]::Parse($expires).AddSeconds(-90))) {
            throw 'The upload window expired. Check the lab state before starting another request.'
        }
        try {
            $response = Invoke-WebRequest $url -Method Post -ContentType 'application/json' `
                -Body $body -Headers @{
                    'x-functions-key' = $key; 'X-Lab-Upload-Id' = $requestId
                    'X-Upload-Profile' = $profile
                } `
                -TimeoutSec 90 -SkipHttpErrorCheck -MaximumRedirection 0 `
                -Verbose:$false -Debug:$false
        } catch {
            Write-Warning 'No upload response received; reconciling using the same request ID.'
            Start-Sleep -Seconds 10
            continue
        }
        if ([int]$response.StatusCode -eq 201) {
            $result = $response.Content | ConvertFrom-Json
            break
        }
        if ([int]$response.StatusCode -in @(410, 503)) {
            Write-Warning 'Upload gate or service not ready; retrying the same request ID within the original window.'
            Start-Sleep -Seconds 10
            continue
        }
        if ([int]$response.StatusCode -eq 409) {
            throw 'The lab reports an existing or uncertain upload. Stop for operator review.'
        }
        throw "Upload refused with HTTP $($response.StatusCode). No credential content is printed."
    }
    if ($result.status -ne 'uploaded') { throw 'Upload was not confirmed; contact the operator.' }
    Write-Host "The $profile token was uploaded. Investigation enablement is unchanged."
} finally {
    $body = $null
    $key = $null
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    if ($null -ne $token) { $token.Dispose() }
    try {
        Invoke-Azure @('functionapp', 'config', 'appsettings', 'set', '-g',
            'NoteHelper_Resources', '-n', 'func-salesbuddy-failure-lab',
            '--settings', 'TOKEN_UPLOAD_EXPIRES_AT=', '--output', 'none') | Out-Null
    } finally {
        if ($uploadRoleId) {
            Invoke-Azure @('role', 'assignment', 'delete', '--ids', $uploadRoleId,
                '--output', 'none') | Out-Null
        }
    }
}
