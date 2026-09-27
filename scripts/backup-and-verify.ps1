param(
    [string]$EnvFile = ".env.local"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$composeArgs = @("compose", "--env-file", $EnvFile, "-f", "docker/compose.yaml")
$restoreCreated = $false
$previousBackupFile = $env:BACKUP_FILE
$previousRestoreConfirm = $env:RESTORE_CONFIRM

Push-Location $repositoryRoot
try {
    $backupOutput = & docker @composeArgs --profile tools run --rm db-backup 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Database backup failed: $($backupOutput -join [Environment]::NewLine)"
    }
    $backupOutput | ForEach-Object { Write-Output $_ }
    $backupLine = $backupOutput | Where-Object { "$_" -match '^backup created: /backup/(trade_[A-Za-z0-9_]+\.dump)$' } | Select-Object -Last 1
    if (-not $backupLine -or "${backupLine}" -notmatch '^backup created: /backup/(trade_[A-Za-z0-9_]+\.dump)$') {
        throw "Could not identify the newly created backup artifact"
    }
    $backupName = $Matches[1]
    $backupPath = Join-Path $repositoryRoot "docker/db-backup/volume/output/$backupName"
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        throw "Backup artifact is missing: $backupPath"
    }

    $runningRestore = & docker @composeArgs --profile restore ps -q postgres-restore
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the isolated restore service" }
    if ($runningRestore) { throw "An isolated restore is already running; retry when it finishes" }
    & docker @composeArgs --profile restore rm -f postgres-restore | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not reset the isolated restore service" }

    $env:BACKUP_FILE = $backupName
    $env:RESTORE_CONFIRM = "isolated"
    $restoreCreated = $true
    & docker @composeArgs --profile restore up -d postgres-restore
    if ($LASTEXITCODE -ne 0) { throw "Could not start the isolated restore service" }
    $restoreOutput = & docker @composeArgs --profile restore run --rm db-restore 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Isolated restore verification failed: $($restoreOutput -join [Environment]::NewLine)"
    }
    $restoreOutput | ForEach-Object { Write-Output $_ }
    if (-not ($restoreOutput | Where-Object { "$_" -match 'restore verification passed:' })) {
        throw "Restore completed without a verification result"
    }

    $hash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $metadataPath = [System.IO.Path]::ChangeExtension($backupPath, ".metadata")
    $recordedHash = (Get-Content -LiteralPath $metadataPath | Where-Object { $_ -match '^dump_sha256=([a-f0-9]{64})$' } | Select-Object -Last 1)
    if (-not $recordedHash -or $recordedHash -notmatch '^dump_sha256=([a-f0-9]{64})$' -or $Matches[1] -ne $hash) {
        throw "Backup archive changed after restore verification"
    }
    $markerPath = "$backupPath.verified"
    @(
        "format=trade-container-verified-v1"
        "dump_sha256=$hash"
        "verified_at_utc=$([DateTime]::UtcNow.ToString('o'))"
    ) | Set-Content -LiteralPath $markerPath -Encoding utf8
    Write-Output "verified backup: $backupPath"
} finally {
    if ($restoreCreated) {
        & docker @composeArgs --profile restore stop postgres-restore | Out-Null
        & docker @composeArgs --profile restore rm -f postgres-restore | Out-Null
    }
    if ($null -eq $previousBackupFile) { Remove-Item Env:BACKUP_FILE -ErrorAction SilentlyContinue }
    else { $env:BACKUP_FILE = $previousBackupFile }
    if ($null -eq $previousRestoreConfirm) { Remove-Item Env:RESTORE_CONFIRM -ErrorAction SilentlyContinue }
    else { $env:RESTORE_CONFIRM = $previousRestoreConfirm }
    Pop-Location
}
