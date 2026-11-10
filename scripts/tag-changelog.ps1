# tag-changelog.ps1
#
# Tag the topmost untagged CHANGELOG.md heading with the short SHA of the
# most recent merge commit on the current branch, then create a "Tag
# changelog for <hash>" commit.
#
# Workflow:
#   1. On a feature branch, make your code change(s) and commit them.
#   2. Edit CHANGELOG.md, prepend a new section like:
#        ## 4/29/2026
#        - what you did
#      Leave the heading without a hash. Commit locally; do not push the branch.
#   3. After the user signs off and the branch is merged into main with
#      `git merge --no-ff` (creating a merge commit), check out main and
#      run this script. It rewrites the topmost untagged heading to:
#        ## 4/29/2026 - <merge-short-sha>
#      and commits the change directly on main as a separate
#      "Tag changelog" commit. Push main only and verify the push.
#   4. While on main, verify the feature branch tip is an ancestor of main
#      (`git merge-base --is-ancestor <branch> main`), then delete the local
#      branch with `git branch -d <branch>`. Never force deletion or delete
#      a branch checked out in another worktree.
#
# Why merge commits: the admin Updates card filters changelog entries by
# commit hash. Tagging the merge commit means a single user-visible
# update maps to a single hash, even if it took multiple commits inside
# the feature branch to build.
#
# Run from the repo root:
#   .\scripts\tag-changelog.ps1
#
# Optional: pass -DryRun to print what would happen without committing.

[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repoRoot = git rev-parse --show-toplevel
if (-not $repoRoot) {
    Write-Error "Not inside a git repository."
    exit 1
}

$changelogPath = Join-Path $repoRoot 'CHANGELOG.md'
if (-not (Test-Path $changelogPath)) {
    Write-Error "CHANGELOG.md not found at $changelogPath"
    exit 1
}

# Find the most recent merge commit on the current branch. This is what
# we tag entries with - one merge = one user-visible update.
$mergeSha = (git log --merges -n 1 --format='%h' HEAD).Trim()
if (-not $mergeSha) {
    Write-Error "No merge commits found on this branch. Tag entries only after merging a feature branch into main."
    exit 1
}
$mergeSubject = (git log -n 1 --format='%s' $mergeSha).Trim()

# Find the topmost ## heading. If it already has a `- <hash>` suffix,
# nothing to do. Otherwise rewrite it in place.
$lines = Get-Content -Path $changelogPath
$headingRegex = '^##\s+(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})(\s+-\s+[0-9a-f]{7,40})?\s*$'

$topIndex = -1
for ($i = 0; $i -lt $lines.Length; $i++) {
    if ($lines[$i] -match $headingRegex) {
        $topIndex = $i
        break
    }
}

if ($topIndex -lt 0) {
    Write-Error "No date heading (## M/D/YYYY) found in CHANGELOG.md."
    exit 1
}

$topLine = $lines[$topIndex]
if ($topLine -match '^##\s+\S+\s+-\s+[0-9a-f]{7,40}\s*$') {
    Write-Host "Topmost heading is already tagged: $topLine"
    Write-Host "Nothing to do."
    exit 0
}

# Pull the date out, rebuild the heading with the hash.
if ($topLine -notmatch '^##\s+(\S+)\s*$') {
    Write-Error "Could not parse top heading: $topLine"
    exit 1
}
$dateText = $Matches[1]
$newHeading = "## $dateText - $mergeSha"

Write-Host "Tagging top entry:"
Write-Host "  Old: $topLine"
Write-Host "  New: $newHeading"
Write-Host "  Hash points to merge commit: $mergeSha ($mergeSubject)"

if ($DryRun) {
    Write-Host "(dry run, no changes written)"
    exit 0
}

$lines[$topIndex] = $newHeading
Set-Content -Path $changelogPath -Value $lines -Encoding utf8

Push-Location $repoRoot
try {
    git add CHANGELOG.md | Out-Null
    git commit -m "Tag changelog for $mergeSha" | Out-Null
    $tagCommit = (git rev-parse --short HEAD).Trim()
    Write-Host "Created tag commit: $tagCommit"
} finally {
    Pop-Location
}
