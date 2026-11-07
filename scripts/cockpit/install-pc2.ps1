<#
.SYNOPSIS
  Installs the cockpit patch (this checkout) over Row-Bot 5.3.0, with a backup first.

.DESCRIPTION
  -AppDir     the Row-Bot folder that holds state.json and versions\<version>\{app,python,version.json}
  -BackupDir  where the backup goes; it must be empty or missing (a backup already there means exit 3)
  -DataDir    Row-Bot's data folder (update_config.json lives here)
  -SourceDir  the checkout to install from (the fork's cockpit branch)
  -DryRun     run every check and say what would happen; changes nothing
  -Restart    afterwards start the H1-RowBot-Restart task (off by default; Row-Bot is never stopped here)

  Exit codes: 0 installed (or, with -DryRun, would install), 1 failed, 2 not Row-Bot 5.3.0, 3 a backup is already there.
  Nothing changes on 1 before the backup, on 2 or on 3. To go back: undo-pc2.ps1 with the same folders.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [switch]$DryRun,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$WantedVersion = '5.3.0'
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

function Copy-Tree([string]$From, [string]$To) {
    # /MIR makes To match From, /COPY:DAT keeps contents, times and flags; codes 0-7 are success for robocopy
    $null = & robocopy $From $To /MIR /COPY:DAT /IS /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XJ
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE copying $From" }
    $global:LASTEXITCODE = 0
}

function Find-Python([string]$VersionDir) {
    $candidates = @((Join-Path $VersionDir 'python\python.exe'), 'python', 'python3')
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $null = & $cmd.Source -I -c 'import argparse, fnmatch, json, shutil' 2>$null
            if ($LASTEXITCODE -eq 0) { return $cmd.Source }
        } catch { }
    }
    return $null
}

try {
    # ---- checks that change nothing: 2 (not 5.3.0), 3 (backup there), then the rest of the inputs
    $statePath = Join-Path $AppDir 'state.json'
    $version = $null
    $active = $null
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $active = [string](Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json).active
        if ($active -match '^[0-9A-Za-z._-]+$') {
            $versionPath = Join-Path $AppDir "versions\$active\version.json"
            if (Test-Path -LiteralPath $versionPath -PathType Leaf) {
                $version = [string](Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json).version
            }
        }
    }
    $versionDir = if ($version) { Join-Path $AppDir "versions\$active" } else { $null }
    if ($version -ne $WantedVersion -or -not (Test-Path -LiteralPath (Join-Path $versionDir 'app') -PathType Container)) {
        Say "Not Row-Bot ${WantedVersion}: found '$version' in $AppDir. Nothing changed."
        exit 2
    }
    $appFolder = Join-Path $versionDir 'app'

    if ((Test-Path -LiteralPath $BackupDir) -and @(Get-ChildItem -LiteralPath $BackupDir -Force).Count -gt 0) {
        Say "A backup is already in $BackupDir. Run undo-pc2.ps1 first. Nothing changed."
        exit 3
    }

    foreach ($required in 'scripts\app_payload_manifest.py', 'src\row_bot') {
        if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $required))) {
            Say "SourceDir $SourceDir has no $required. Nothing changed."
            exit 1
        }
    }
    $python = Find-Python $versionDir
    if (-not $python) { Say 'No Python found to stage the files (Row-Bot''s own python is missing and none is on PATH). Nothing changed.'; exit 1 }

    $configPath = Join-Path $DataDir 'update_config.json'
    $config = $null
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try { $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
        catch { Say "$configPath is not valid JSON. Nothing changed."; exit 1 }
        if ($config -isnot [System.Management.Automation.PSCustomObject]) { Say "$configPath is not a JSON object. Nothing changed."; exit 1 }
    }
    $frontend = Join-Path $SourceDir 'src\row_bot\static\client-v2'
    $haveFrontend = Test-Path -LiteralPath (Join-Path $frontend 'index.html') -PathType Leaf

    if ($DryRun) {
        Say "Dry run: Row-Bot $version is in $AppDir and no backup is in $BackupDir."
        Say "Would back up $appFolder and $configPath to $BackupDir."
        Say "Would stage $SourceDir with $python and put it into $appFolder (delete, then write each file; old __pycache__ removed)."
        if ($haveFrontend) { Say 'Would add the built Talk screen (static\client-v2) from the checkout.' }
        else { Say 'WARNING: the checkout has no built Talk screen (src\row_bot\static\client-v2); it would be left as it is.' }
        Say 'Would set auto_check and auto_download to false in update_config.json, keeping its other keys.'
        Say 'Dry run: nothing was changed.'
        exit 0
    }

    # ---- stage first (a temp folder), so a staging failure leaves the app untouched and no backup behind
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ('rowbot-h2-stage-' + [guid]::NewGuid().ToString('N'))
    try {
        $null = & $python -I (Join-Path $SourceDir 'scripts\app_payload_manifest.py') --project-root $SourceDir --stage $stage
        if ($LASTEXITCODE -ne 0) { throw "staging the checkout failed (code $LASTEXITCODE)" }
        if ($haveFrontend) {
            $stagedFrontend = Join-Path $stage 'src\row_bot\static\client-v2'
            Copy-Item -LiteralPath $frontend -Destination $stagedFrontend -Recurse -Force
        } else {
            Say 'WARNING: no built Talk screen in the checkout (src\row_bot\static\client-v2); the app keeps its old one. Build it with npm --prefix frontend run build.'
        }

        # ---- backup first, verified, marker last
        $configExisted = Test-Path -LiteralPath $configPath -PathType Leaf
        $dataDirExisted = Test-Path -LiteralPath $DataDir -PathType Container
        New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
        try {
            Copy-Tree $appFolder (Join-Path $BackupDir 'app')
            if (-not (Test-SameTree $appFolder (Join-Path $BackupDir 'app'))) { throw 'the backup does not match the app folder' }
            if ($configExisted) { Copy-Item -LiteralPath $configPath -Destination (Join-Path $BackupDir 'update_config.json') }
            @{ version = $version; active = $active; app = $appFolder; config = $configPath; config_existed = $configExisted
               data_dir_existed = $dataDirExisted; made = (Get-Date).ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BackupDir 'backup.json') -Encoding utf8
        } catch {
            Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
            throw
        }
        Say "Backed up $appFolder to $BackupDir."

        # ---- put the patch in: delete, then write, because later versions may share files by hard link
        if ($haveFrontend) {
            $oldFrontend = Join-Path $appFolder 'src\row_bot\static\client-v2'
            if (Test-Path -LiteralPath $oldFrontend) { Remove-Item -LiteralPath $oldFrontend -Recurse -Force }
        }
        $stageFull = (Get-Item -LiteralPath $stage).FullName.TrimEnd('\')
        foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File -Force) {
            $target = Join-Path $appFolder $file.FullName.Substring($stageFull.Length + 1)
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
            Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        }
        # Row-Bot ships "unchecked-hash" bytecode that Python never rechecks; stale copies would keep the old code running
        $stale = @(Get-ChildItem -LiteralPath $appFolder -Recurse -Directory -Force -Filter '__pycache__')
        foreach ($folder in $stale) { Remove-Item -LiteralPath $folder.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }

    # ---- Row-Bot's own updates stay off, other keys kept
    if ($null -eq $config) { $config = [pscustomobject]@{} }
    foreach ($flag in 'auto_check', 'auto_download') {
        if ($config.PSObject.Properties[$flag]) { $config.$flag = $false } else { $config | Add-Member -NotePropertyName $flag -NotePropertyValue $false }
    }
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    [System.IO.File]::WriteAllText($configPath, (($config | ConvertTo-Json -Depth 20) + "`n"), (New-Object System.Text.UTF8Encoding($false)))

    Say "Installed the cockpit patch into $appFolder. Restart Row-Bot to use it. To go back: undo-pc2.ps1 with the same folders."
    if ($Restart) {
        Start-ScheduledTask -TaskName $RestartTask
        Say "Started the $RestartTask task."
    }
    exit 0
} catch {
    Write-Output "Install failed: $($_.Exception.Message)"
    if (Test-Path -LiteralPath (Join-Path $BackupDir 'backup.json')) { Write-Output 'The backup is complete: run undo-pc2.ps1 to put everything back.' }
    exit 1
}
