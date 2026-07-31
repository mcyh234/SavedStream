param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

function Read-SecretText([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Set-EnvValue([System.Collections.Generic.List[string]]$Lines, [string]$Key, [string]$Value) {
    $escaped = [regex]::Escape($Key)
    $index = -1
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match "^\s*$escaped=") {
            $index = $i
            break
        }
    }
    $line = "$Key=$Value"
    if ($index -ge 0) {
        $Lines[$index] = $line
    } else {
        [void]$Lines.Add($line)
    }
}

$apiIdRaw = Read-Host "TELEGRAM_API_ID (from my.telegram.org/apps)"
if ($apiIdRaw -notmatch "^[1-9][0-9]*$") {
    throw "TELEGRAM_API_ID must be a positive integer."
}

$apiHash = Read-SecretText "TELEGRAM_API_HASH (hidden input)"
if ([string]::IsNullOrWhiteSpace($apiHash)) {
    throw "TELEGRAM_API_HASH cannot be empty."
}

$adminKey = Read-SecretText "ADMIN_KEY (hidden; press Enter to generate)"
if ([string]::IsNullOrWhiteSpace($adminKey)) {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $adminKey = [Convert]::ToHexString($bytes).ToLowerInvariant()
}

$envPath = Join-Path $ProjectRoot ".env"
$lines = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        [void]$lines.Add($line)
    }
}

Set-EnvValue $lines "TELEGRAM_API_ID" $apiIdRaw
Set-EnvValue $lines "TELEGRAM_API_HASH" $apiHash
Set-EnvValue $lines "ADMIN_KEY" $adminKey

[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))

Write-Host "Saved Telegram configuration to $envPath"
Write-Host "Credentials were not printed. .env is already gitignored."
Write-Host "Start with: docker compose up -d --build"
