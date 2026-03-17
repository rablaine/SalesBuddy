# Sales Buddy - Scheduled Milestone Sync
# Called by the SalesBuddy-MilestoneSync Windows scheduled task.
# Hits the local Flask API to trigger a non-SSE milestone sync.
#
# Usage:
#   .\scripts\milestone-sync.ps1          (reads PORT from .env, defaults to 5151)

param()

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

# Read port from .env
$Port = 5151
if (Test-Path "$RepoRoot\.env") {
    $envLines = Get-Content "$RepoRoot\.env" -ErrorAction SilentlyContinue
    foreach ($line in $envLines) {
        if ($line -match '^\s*PORT\s*=\s*(\d+)') {
            $Port = [int]$Matches[1]
        }
    }
}

$Url = "http://localhost:$Port/api/milestone-tracker/sync"

# Check if server is running
try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) {
        Write-Host "Sales Buddy is not running on port $Port - skipping sync." -ForegroundColor Yellow
        exit 0
    }
} catch {
    Write-Host "Could not check port $Port - skipping sync." -ForegroundColor Yellow
    exit 0
}

# Trigger sync (JSON fallback, not SSE)
try {
    Write-Host "Triggering milestone sync at $Url ..."
    $response = Invoke-RestMethod -Uri $Url -Method POST -ContentType 'application/json' -TimeoutSec 600
    if ($response.success) {
        Write-Host "Sync complete: $($response.customers_synced) customers, $($response.milestones_created) new, $($response.milestones_updated) updated." -ForegroundColor Green
    } else {
        Write-Host "Sync returned partial results or failed: $($response.error)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Milestone sync request failed: $_" -ForegroundColor Red
    exit 1
}
