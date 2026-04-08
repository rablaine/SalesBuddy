# Marketing Insights Access Audit
# Run this script to diagnose why Marketing Insights shows "No Marketing Data".
# Prerequisites: az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47

$ErrorActionPreference = "Stop"

Write-Host "`n=== Marketing Insights Access Audit ===" -ForegroundColor Cyan
Write-Host "Getting CRM token..." -ForegroundColor Gray

try {
    $token = az account get-access-token --resource "https://microsoftsales.crm.dynamics.com" --tenant "72f988bf-86f1-41af-91ab-2d7cd011db47" --query accessToken -o tsv
    if (-not $token) { throw "No token returned" }
} catch {
    Write-Host "FAIL: Could not get CRM token. Run: az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47" -ForegroundColor Red
    exit 1
}

$headers = @{ Authorization = "Bearer $token" }
$base = "https://microsoftsales.crm.dynamics.com/api/data/v9.2"

# 1. Identity
Write-Host "`n--- 1. Your CRM Identity ---" -ForegroundColor Yellow
$whoami = Invoke-RestMethod -Uri "$base/WhoAmI" -Headers $headers
$userId = $whoami.UserId
$user = Invoke-RestMethod -Uri "$base/systemusers($userId)?`$select=fullname,internalemailaddress,title" -Headers $headers
Write-Host "  Name:  $($user.fullname)"
Write-Host "  Email: $($user.internalemailaddress)"
Write-Host "  Title: $($user.title)"
Write-Host "  BU:    $($whoami.BusinessUnitId)"

# 2. Security Roles
Write-Host "`n--- 2. Your Security Roles ---" -ForegroundColor Yellow
$roles = (Invoke-RestMethod -Uri "$base/systemusers($userId)/systemuserroles_association?`$select=name,roleid" -Headers $headers).value
foreach ($role in $roles | Sort-Object name) {
    Write-Host "  $($role.name)"
}

# 3. Marketing entity read test
Write-Host "`n--- 3. Entity Read Tests ---" -ForegroundColor Yellow

$entities = @(
    @{ Name = "msp_marketingengagements"; Label = "Marketing Summaries" },
    @{ Name = "msp_marketinginteractions"; Label = "Marketing Interactions" },
    @{ Name = "contacts"; Label = "Contacts (baseline)" }
)

foreach ($ent in $entities) {
    try {
        $r = Invoke-RestMethod -Uri "$base/$($ent.Name)?`$top=1&`$select=createdon" -Headers $headers
        $count = $r.value.Count
        if ($count -gt 0) {
            Write-Host "  $($ent.Label): OK (can read)" -ForegroundColor Green
        } else {
            Write-Host "  $($ent.Label): EMPTY (query succeeded but 0 rows visible)" -ForegroundColor Red
        }
    } catch {
        Write-Host "  $($ent.Label): DENIED ($($_.Exception.Message))" -ForegroundColor Red
    }
}

# 4. Spot-check specific TPIDs
Write-Host "`n--- 4. TPID Spot-Check (5 known-good TPIDs) ---" -ForegroundColor Yellow
$testTpids = @(
    @{ tpid = "14776593"; name = "LIVEPERSON INC" },
    @{ tpid = "670376";   name = "STELLANTIS FINANCIAL" },
    @{ tpid = "664316";   name = "WESTON AND SAMPSON" },
    @{ tpid = "8510578";  name = "ANA (ANCC)" },
    @{ tpid = "4724631";  name = "CONSIGLI CONSTRUCTION" }
)

$found = 0
foreach ($t in $testTpids) {
    $r = Invoke-RestMethod -Uri "$base/msp_marketingengagements?`$filter=msp_mstopparentid eq '$($t.tpid)'&`$select=msp_mstopparentid&`$top=1" -Headers $headers
    if ($r.value.Count -gt 0) {
        Write-Host "  $($t.name) (TPID $($t.tpid)): FOUND" -ForegroundColor Green
        $found++
    } else {
        Write-Host "  $($t.name) (TPID $($t.tpid)): NOT VISIBLE" -ForegroundColor Red
    }
}

# 5. Summary
Write-Host "`n--- 5. Diagnosis ---" -ForegroundColor Yellow
if ($found -eq 5) {
    Write-Host "  All TPIDs visible - marketing sync should work. Re-run the sync." -ForegroundColor Green
} elseif ($found -gt 0) {
    Write-Host "  Partial access ($found/5 TPIDs visible) - unusual. Share this output with Alex." -ForegroundColor Yellow
} else {
    Write-Host "  No marketing engagement records are visible to your account." -ForegroundColor Red
    Write-Host "  You need a security role that grants Read on the msp_marketingengagement entity." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Action: Request one of these roles from your CRM admin:" -ForegroundColor Cyan
    Write-Host "    - Microsoft Sales Manager" -ForegroundColor White
    Write-Host "    - MSP Seller Manager" -ForegroundColor White
    Write-Host "  Or ask to be added to the same Dataverse team/AAD security group as Alex Blaine." -ForegroundColor White
}

Write-Host "`n=== Audit Complete ===" -ForegroundColor Cyan
Write-Host "Send this output to Alex if you need help.`n"
