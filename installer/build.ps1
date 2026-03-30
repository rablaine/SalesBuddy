<#
.SYNOPSIS
    Builds the Sales Buddy MSI installer.
.DESCRIPTION
    Checks prerequisites (.NET SDK, WiX CLI tool), runs dotnet build,
    and copies the MSI to installer/output/.
.EXAMPLE
    .\build.ps1
    .\build.ps1 -Configuration Debug
#>
param(
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Sales Buddy MSI Build ===" -ForegroundColor Cyan

# --- Check .NET SDK ---
Write-Host "`nChecking .NET SDK..." -ForegroundColor Yellow
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Host "ERROR: .NET SDK not found. Install from https://dotnet.microsoft.com/download" -ForegroundColor Red
    exit 1
}
$sdkVersion = & dotnet --version 2>&1
$major = [int]($sdkVersion -split '\.')[0]
if ($major -lt 8) {
    Write-Host "ERROR: .NET SDK 8.0+ required (found $sdkVersion)" -ForegroundColor Red
    exit 1
}
Write-Host "  .NET SDK $sdkVersion" -ForegroundColor Green

# --- Check WiX CLI tool ---
Write-Host "Checking WiX CLI tool..." -ForegroundColor Yellow
$wixInstalled = & dotnet tool list -g 2>&1 | Select-String "wix\s"
if (-not $wixInstalled) {
    Write-Host "  WiX not found. Installing..." -ForegroundColor Yellow
    & dotnet tool install -g wix
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install WiX CLI tool" -ForegroundColor Red
        exit 1
    }
    Write-Host "  WiX installed" -ForegroundColor Green
} else {
    $wixLine = $wixInstalled.ToString().Trim()
    Write-Host "  $wixLine" -ForegroundColor Green
}

# --- Build ---
Write-Host "`nBuilding MSI ($Configuration)..." -ForegroundColor Yellow
Push-Location $scriptDir
try {
    & dotnet build -c $Configuration
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nERROR: Build failed" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

# --- Copy MSI to output/ ---
$binDir = Join-Path $scriptDir "bin" $Configuration
$msiFiles = Get-ChildItem -Path $binDir -Filter "*.msi" -Recurse -ErrorAction SilentlyContinue
if (-not $msiFiles -or $msiFiles.Count -eq 0) {
    Write-Host "ERROR: No .msi found in $binDir" -ForegroundColor Red
    exit 1
}

$outputDir = Join-Path $scriptDir "output"
if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

foreach ($msi in $msiFiles) {
    Copy-Item $msi.FullName -Destination $outputDir -Force
    Write-Host "`nMSI copied to: $(Join-Path $outputDir $msi.Name)" -ForegroundColor Green
}

Write-Host "`n=== Build complete ===" -ForegroundColor Cyan
