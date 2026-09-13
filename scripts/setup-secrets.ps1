# setup-secrets.ps1 - Setup local development and Paper testing secrets for Trade_Container
[CmdletBinding()]
param (
    [string]$SecretsRoot = "$HOME\bot\secrets"
)

$ErrorActionPreference = "Stop"

$dbSecretDir = Join-Path $SecretsRoot "local\database"
$apiSecretDir = Join-Path $SecretsRoot "local\trade-api"

New-Item -ItemType Directory -Force -Path $dbSecretDir | Out-Null
New-Item -ItemType Directory -Force -Path $apiSecretDir | Out-Null

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

$pgPasswordFile = Join-Path $dbSecretDir "postgres_password"
if (-not (Test-Path $pgPasswordFile)) {
    $bytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    [System.IO.File]::WriteAllText($pgPasswordFile, $hex, $utf8NoBom)
    Write-Host "Created: $pgPasswordFile"
} else {
    Write-Host "Already exists: $pgPasswordFile"
}

$apiTokenFile = Join-Path $apiSecretDir "api_token"
if (-not (Test-Path $apiTokenFile)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    [System.IO.File]::WriteAllText($apiTokenFile, $hex, $utf8NoBom)
    Write-Host "Created: $apiTokenFile"
} else {
    Write-Host "Already exists: $apiTokenFile"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$envLocalFile = Join-Path $repoRoot ".env.local"
$envContent = "TRADE_SECRETS_DIR=$SecretsRoot`n"
[System.IO.File]::WriteAllText($envLocalFile, $envContent, $utf8NoBom)
Write-Host "Created: $envLocalFile"
Write-Host ""
Write-Host "Setup complete. You can now start Trade_Container with:"
Write-Host "  docker compose --env-file .env.local -f docker/compose.yaml up -d"
