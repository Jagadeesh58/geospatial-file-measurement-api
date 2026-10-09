# This one-time helper attaches the extracted source snapshot to the existing
# public repository's main branch without running git clone on this computer.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptName = Split-Path -Leaf $MyInvocation.MyCommand.Path
$expectedOrigin = 'https://github.com/Jagadeesh58/geospatial-file-measurement-api.git'
$gitConfig = Join-Path $repoRoot '.git\config'

if (-not (Test-Path -LiteralPath $gitConfig)) {
    throw 'Git metadata was not found. Extract the complete ZIP and run this script from its project folder.'
}

$actualOrigin = (& git -C $repoRoot remote get-url origin 2>$null | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or $actualOrigin.TrimEnd('/') -ne $expectedOrigin.TrimEnd('/')) {
    throw "The configured origin is not the expected repository: $expectedOrigin"
}

Write-Host ''
Write-Host 'This will fetch the existing main branch and place the ZIP source files on top of it.' -ForegroundColor Yellow
Write-Host 'It does not clone the repository, commit changes, or push anything.'
Write-Host 'Run this only in the freshly extracted project folder.'
$confirmation = Read-Host 'Type UPDATE to continue'
if ($confirmation -cne 'UPDATE') {
    Write-Host 'Cancelled. No project files were changed.'
    exit 1
}

$temporaryRoot = Join-Path $env:TEMP ('geospatial-api-setup-' + [guid]::NewGuid().ToString('N'))
$snapshot = Join-Path $temporaryRoot 'source-snapshot'
$completed = $false
New-Item -ItemType Directory -Path $snapshot -Force | Out-Null

try {
    # Keep a copy of the source snapshot before the working tree is cleared.
    $excludedGit = Join-Path $repoRoot '.git'
    $excludedVenv = Join-Path $repoRoot '.venv'
    & robocopy $repoRoot $snapshot /E /COPY:DAT /R:1 /W:1 /XD $excludedGit $excludedVenv /XF $scriptName | Out-Host
    if ($LASTEXITCODE -ge 8) {
        throw 'Could not stage the extracted source snapshot for setup.'
    }

    # Clear the extracted source files so Git can safely check out the remote main branch.
    Get-ChildItem -LiteralPath $repoRoot -Force |
        Where-Object { $_.Name -ne '.git' -and $_.Name -ne $scriptName } |
        Remove-Item -Recurse -Force

    & git -C $repoRoot fetch origin '+refs/heads/main:refs/remotes/origin/main'
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not fetch origin/main. Check your internet connection and GitHub access.'
    }

    & git -C $repoRoot checkout -B main origin/main
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not switch to the existing main branch.'
    }
    & git -C $repoRoot branch --set-upstream-to=origin/main main | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not configure main to track origin/main.'
    }

    # Remove tracked files from the old version when they are not present in the new snapshot.
    $snapshotPrefix = $snapshot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $snapshotFiles = Get-ChildItem -LiteralPath $snapshot -Recurse -File | ForEach-Object {
        $_.FullName.Substring($snapshotPrefix.Length).Replace('\', '/')
    }
    $snapshotSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relativePath in $snapshotFiles) { [void]$snapshotSet.Add($relativePath) }

    $trackedFiles = & git -C $repoRoot ls-files
    if ($LASTEXITCODE -ne 0) { throw 'Could not list files tracked by the repository.' }
    foreach ($trackedPath in $trackedFiles) {
        $normalized = $trackedPath.Replace('\', '/')
        if (-not $snapshotSet.Contains($normalized)) {
            $oldPath = Join-Path $repoRoot $trackedPath
            if (Test-Path -LiteralPath $oldPath -PathType Leaf) {
                Remove-Item -LiteralPath $oldPath -Force
            }
        }
    }

    # Overlay the updated source; robocopy exit codes 0-7 are non-fatal.
    & robocopy $snapshot $repoRoot /E /COPY:DAT /R:1 /W:1 | Out-Host
    if ($LASTEXITCODE -ge 8) { throw 'Could not copy the updated source files into the repository.' }

    Remove-Item -LiteralPath (Join-Path $repoRoot $scriptName) -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host 'Setup complete. Existing Git history is preserved and source changes are unstaged.' -ForegroundColor Green
    Write-Host 'Review changes with: git status --short'
    Write-Host 'No add, commit, or push command was run.'
    & git -C $repoRoot status --short
    if ($LASTEXITCODE -ne 0) { throw 'Git status failed; inspect the repository before continuing.' }
    $completed = $true
}
catch {
    Write-Host ''
    Write-Host ('Setup stopped: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Restoring the extracted source files so the project remains available.' -ForegroundColor Yellow
    try {
        Get-ChildItem -LiteralPath $repoRoot -Force |
            Where-Object { $_.Name -ne '.git' -and $_.Name -ne $scriptName } |
            Remove-Item -Recurse -Force
        & robocopy $snapshot $repoRoot /E /COPY:DAT /R:1 /W:1 | Out-Host
    } catch {
        Write-Host ('Automatic restore was incomplete. The source snapshot remains at: ' + $snapshot) -ForegroundColor Yellow
    }
    Write-Host 'Do not commit or push until the working tree has been reviewed.'
    throw
}
finally {
    # Keep the recovery snapshot only when setup did not finish.
    if ($completed) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
