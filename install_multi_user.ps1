$ErrorActionPreference = "Stop"

$ProjectPath = "C:\Users\Awele\Documents\Vinted-Notifications"
$PackagePath = Split-Path -Parent $MyInvocation.MyCommand.Path
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupPath = Join-Path $ProjectPath "backup-multi-user-$Timestamp"

Write-Host ""
Write-Host "Vinted Notifications multi-user installer"
Write-Host "Project: $ProjectPath"
Write-Host "Package: $PackagePath"
Write-Host ""

if (-not (Test-Path $ProjectPath)) {
    throw "Project folder not found: $ProjectPath"
}

$running = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match "^python" -and
        $_.CommandLine -like "*vinted_notifications.py*"
    }

if ($running) {
    throw "Vinted Notifications is still running. Stop it with Ctrl+C, then run this installer again."
}

New-Item -ItemType Directory -Path $BackupPath -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $BackupPath "telegram_bot_plugin") -Force | Out-Null

$files = @(
    "db.py",
    "core.py",
    "initial_db.sql",
    "vinted_notifications.py",
    "url_normalizer.py"
)

foreach ($file in $files) {
    $existing = Join-Path $ProjectPath $file
    if (Test-Path $existing) {
        Copy-Item $existing (Join-Path $BackupPath $file) -Force
    }
}

$telegramExisting = Join-Path $ProjectPath "telegram_bot_plugin\telegram_bot.py"
if (Test-Path $telegramExisting) {
    Copy-Item $telegramExisting (Join-Path $BackupPath "telegram_bot_plugin\telegram_bot.py") -Force
}

$database = Join-Path $ProjectPath "data\vinted_notifications.db"
if (Test-Path $database) {
    New-Item -ItemType Directory -Path (Join-Path $BackupPath "data") -Force | Out-Null
    Copy-Item $database (Join-Path $BackupPath "data\vinted_notifications.db") -Force
}

foreach ($file in $files) {
    Copy-Item (Join-Path $PackagePath $file) (Join-Path $ProjectPath $file) -Force
}

New-Item -ItemType Directory -Path (Join-Path $ProjectPath "telegram_bot_plugin") -Force | Out-Null
Copy-Item `
    (Join-Path $PackagePath "telegram_bot_plugin\telegram_bot.py") `
    (Join-Path $ProjectPath "telegram_bot_plugin\telegram_bot.py") `
    -Force

Write-Host ""
Write-Host "Installation completed."
Write-Host "Backup saved to:"
Write-Host $BackupPath
Write-Host ""
Write-Host "Start the app with:"
Write-Host "cd $ProjectPath"
Write-Host "python vinted_notifications.py"
Write-Host ""
