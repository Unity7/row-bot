<#
.SYNOPSIS
  Puts Row-Bot back the way install-pc2.ps1 found it, from its backup, and removes the backup.

.DESCRIPTION
  -AppDir     the Row-Bot folder that holds state.json and versions\<version>\...
  -BackupDir  the folder install-pc2.ps1 backed up to
  -DataDir    Row-Bot's data folder (update_config.json lives here)
  -Restart    afterwards start the H1-RowBot-Restart task (off by default; Row-Bot is never stopped here)

  The app folder is deleted and restored from the backup, so files the install added and any bytecode written
  since are gone. update_config.json comes back as it was, or is deleted if there was none. state.json and
  version.json are never touched.

  Exit codes: 0 restored and backup removed, 1 failed (the backup is kept), 4 there is no backup.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$RestartTask = 'H1-RowBot-Restart'

function Say([string]$Text) { Write-Output $Text }

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try { return [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($stream)) }
    finally { $stream.Dispose() }
}

function Get-TreeHashes([string]$Root) {
    $rootFull = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\', '/')
    $map = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $map[$file.FullName.Substring($rootFull.Length + 1).Replace('\', '/')] = Get-Sha256 $file.FullName
    }
    return $map
}

function Test-SameTree([string]$A, [string]$B) {
    $x = Get-TreeHashes $A
    $y = Get-TreeHashes $B
    if ($x.Count -ne $y.Count) { return $false }
    foreach ($key in $x.Keys) { if (-not $y.ContainsKey($key) -or $x[$key] -ne $y[$key]) { return $false } }
    return $true
}

try {
    $markerPath = Join-Path $BackupDir 'backup.json'
    $savedApp = Join-Path $BackupDir 'app'
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or -not (Test-Path -LiteralPath $savedApp -PathType Container)) {
        Say "No backup in $BackupDir. Nothing changed."
        exit 4
    }
    $backup = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    $active = [string]$backup.active
    if ($active -notmatch '^[0-9A-Za-z._-]+$') { throw "the backup note names no valid version folder ('$active')" }
    $appFolder = Join-Path $AppDir "versions\$active\app"
    $configPath = Join-Path $DataDir 'update_config.json'

    # the app folder: delete it, then restore the backup wholesale (a leftover from a locked file is overwritten by /IS)
    if (Test-Path -LiteralPath $appFolder) { Remove-Item -LiteralPath $appFolder -Recurse -Force -ErrorAction SilentlyContinue }
    $null = & robocopy $savedApp $appFolder /MIR /COPY:DAT /IS /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XJ
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE restoring $appFolder" }
    $global:LASTEXITCODE = 0
    if (-not (Test-SameTree $savedApp $appFolder)) { throw "the restored $appFolder does not match the backup (is Row-Bot still running? stop it and run undo again)" }

    # update_config.json: as it was, or gone if there was none
    $savedConfig = Join-Path $BackupDir 'update_config.json'
    if ($backup.config_existed) {
        if (-not (Test-Path -LiteralPath $savedConfig -PathType Leaf)) { throw 'the backup lost update_config.json' }
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        Copy-Item -LiteralPath $savedConfig -Destination $configPath -Force
    } elseif (Test-Path -LiteralPath $configPath) {
        Remove-Item -LiteralPath $configPath -Force
    }
    if (-not $backup.data_dir_existed -and (Test-Path -LiteralPath $DataDir) -and @(Get-ChildItem -LiteralPath $DataDir -Force).Count -eq 0) {
        Remove-Item -LiteralPath $DataDir -Force
    }

    Remove-Item -LiteralPath $BackupDir -Recurse -Force
    Say "Restored $appFolder and update_config.json from the backup and removed the backup."
    if ($Restart) {
        Start-ScheduledTask -TaskName $RestartTask
        Say "Started the $RestartTask task."
    }
    exit 0
} catch {
    Write-Output "Undo failed: $($_.Exception.Message)"
    Write-Output 'The backup is kept.'
    exit 1
}
