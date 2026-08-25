[CmdletBinding()]
param(
    [string]$Server,
    [string]$SshUser = "root",
    [string]$Domain,
    [ValidateRange(1, 2)]
    [int]$KeepBackups = 2,
    [string]$PrebuiltImageArchive,
    [switch]$AllowServerBuild,
    [switch]$PackageOnly,
    [switch]$ResetConfig
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function New-HexSecret {
    param([int]$Bytes = 32)

    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    }
    finally {
        $rng.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function Read-DeploymentConfig {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    try {
        $saved = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $savedPassword = ConvertTo-SecureString ([string]$saved.password)
        return [pscustomobject]@{
            Server = [string]$saved.server
            SshUser = [string]$saved.ssh_user
            Domain = [string]$saved.domain
            Password = $savedPassword
        }
    }
    catch {
        Write-Warning "Saved deployment configuration could not be decrypted and will be replaced."
        return $null
    }
}

function Save-DeploymentConfig {
    param(
        [string]$Path,
        [string]$Server,
        [string]$SshUser,
        [string]$Domain,
        [Security.SecureString]$Password
    )

    $payload = [ordered]@{
        server = $Server
        ssh_user = $SshUser
        domain = $Domain
        password = ConvertFrom-SecureString $Password
        saved_at = (Get-Date).ToString("o")
    } | ConvertTo-Json
    [IO.File]::WriteAllText($Path, $payload, [Text.UTF8Encoding]::new($false))
    try {
        (Get-Item -LiteralPath $Path).Attributes =
            (Get-Item -LiteralPath $Path).Attributes -bor [IO.FileAttributes]::Hidden
    }
    catch {}
}

function Ensure-PoshSsh {
    if (Get-Module -ListAvailable -Name Posh-SSH) {
        Import-Module Posh-SSH -ErrorAction Stop
        return
    }

    Write-Host "Installing Posh-SSH for the current user..." -ForegroundColor Cyan
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
        Install-PackageProvider -Name NuGet -Scope CurrentUser -Force | Out-Null
    }
    Install-Module Posh-SSH -Scope CurrentUser -Force -AllowClobber -Repository PSGallery
    Import-Module Posh-SSH -ErrorAction Stop
}

function Assert-Inputs {
    if ($Server -notmatch '^[A-Za-z0-9.-]+$') {
        throw "Invalid server IP or host name."
    }
    if ($SshUser -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$') {
        throw "Invalid SSH user name."
    }
    if ($Domain -and $Domain -notmatch '^[A-Za-z0-9.-]+$') {
        throw "Invalid domain. Enter a host such as example.com without a URL path."
    }
}

function New-DeploymentArchive {
    param(
        [string]$Root,
        [string]$Archive
    )

    Write-Host "Packaging application..." -ForegroundColor Cyan
    Push-Location $Root
    try {
        $tarArgs = @(
            "--options", "gzip:compression-level=3",
            "-czf", $Archive,
            "--exclude=_src/.env",
            "--exclude=_src/frontend/node_modules",
            "--exclude=_src/frontend/dist",
            "--exclude=_src/backend/.pytest_cache",
            "--exclude=_src/backend/*/__pycache__",
            "--exclude=_src/backend/*/*/__pycache__",
            "--exclude=TeleBox/.git",
            "--exclude=TeleBox/node_modules",
            "--exclude=TeleBox/config.json",
            "--exclude=TeleBox/.env",
            "--exclude=TeleBox/logs",
            "--exclude=TeleBox/temp",
            "_src", "TeleBox"
        )
        & tar.exe @tarArgs
        if ($LASTEXITCODE -ne 0) {
            throw "tar.exe failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

function Assert-LocalDocker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Local Docker is required. Install and start Docker Desktop, then run this script again."
    }

    $dockerOs = & docker version --format '{{.Server.Os}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or ($dockerOs | Select-Object -Last 1) -ne "linux") {
        throw "Docker Desktop must be running with the Linux container engine."
    }
}

function Get-ImageTarMember {
    param([string]$Archive)

    if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
        throw "Prebuilt image archive not found: $Archive"
    }

    $entries = @(& tar.exe -tzf $Archive 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "Prebuilt image archive is not a valid gzip-compressed tar file: $Archive"
    }

    $member = $entries |
        ForEach-Object { [string]$_ } |
        Where-Object { $_ -and $_ -notmatch '/$' -and $_ -match '\.tar$' } |
        Select-Object -First 1
    if (-not $member) {
        throw "Prebuilt image archive must contain a Docker image tar member (*.tar)."
    }
    if ($member -notmatch '^[A-Za-z0-9._/-]+$' -or $member -match '(^|/)\.\.(?:/|$)') {
        throw "Prebuilt image archive contains an unsafe member name: $member"
    }
    return $member
}

function Get-PrebuiltImageTags {
    param(
        [string]$Archive,
        [string]$ImageTarMember
    )

    $inspectRoot = Join-Path $env:TEMP ("tube-image-inspect-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $inspectRoot -Force | Out-Null
    try {
        & tar.exe -xzf $Archive -C $inspectRoot $ImageTarMember
        if ($LASTEXITCODE -ne 0) {
            throw "Prebuilt image archive does not contain a readable Docker image tar member."
        }
        $innerTarPath = Join-Path $inspectRoot ($ImageTarMember -replace '/', [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $innerTarPath -PathType Leaf)) {
            throw "Extracted Docker image tar member was not found."
        }
        $manifestText = @(& tar.exe -xOf $innerTarPath manifest.json 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0 -or -not $manifestText) {
            throw "Prebuilt image archive does not contain a readable Docker manifest.json."
        }
    }
    finally {
        Remove-Item -LiteralPath $inspectRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    try {
        $manifest = @($manifestText | ConvertFrom-Json)
    }
    catch {
        throw "Prebuilt image archive contains invalid Docker manifest.json."
    }

    $tags = @($manifest | ForEach-Object { @($_.RepoTags) } | Where-Object { $_ })
    $savedTag = $tags | Where-Object { $_ -match '(?i)savedstream' } | Select-Object -First 1
    $teleBoxTag = $tags | Where-Object { $_ -match '(?i)telebox' } | Select-Object -First 1
    if (-not $savedTag -or -not $teleBoxTag) {
        throw "Prebuilt image archive must contain RepoTags matching savedstream and telebox."
    }
    foreach ($tag in @($savedTag, $teleBoxTag)) {
        if ($tag -notmatch '^[A-Za-z0-9._:/-]+$' -or $tag -notmatch ':') {
            throw "Prebuilt image archive contains an unsafe image tag: $tag"
        }
    }
    return [pscustomobject]@{
        SavedStream = [string]$savedTag
        TeleBox = [string]$teleBoxTag
    }
}

function Invoke-RemoteDeployment {
    param(
        [int]$SessionId,
        [string]$Command,
        [int]$TimeoutSeconds = 3600
    )

    $remoteStatus = $null
    $lines = New-Object System.Collections.Generic.List[string]
    $streamCommand = Get-Command Invoke-SSHCommandStream -ErrorAction SilentlyContinue
    $supportsExitStatus = $streamCommand -and $streamCommand.Parameters.ContainsKey('ExitStatusVariable')

    if ($supportsExitStatus) {
        Write-Progress -Id 1 -Activity "Deploying to server" -Status "Connected; waiting for remote deployment output..." -PercentComplete 0
        @(& Invoke-SSHCommandStream -SessionId $SessionId -Command $Command -TimeOut $TimeoutSeconds -ExitStatusVariable remoteStatus |
            ForEach-Object {
                $line = [string]$_
                [void]$lines.Add($line)
                if ($line -match '^__PROGRESS__\|(\d{1,3})\|(.*)$') {
                    $percent = [Math]::Min(100, [Math]::Max(0, [int]$Matches[1]))
                    $status = $Matches[2]
                    Write-Progress -Id 1 -Activity "Deploying to server" -Status $status -PercentComplete $percent
                    Write-Host $status -ForegroundColor DarkCyan
                }
                elseif ($line) {
                    Write-Host $line
                }
                $line
            }) | Out-Null
        Write-Progress -Id 1 -Activity "Deploying to server" -Completed
    }
    else {
        Write-Warning "Installed Posh-SSH does not provide Invoke-SSHCommandStream; remote output will be shown after completion."
        Write-Progress -Id 1 -Activity "Deploying to server" -Status "Remote deployment is running..." -PercentComplete 0
        $fallback = Invoke-SSHCommand -SessionId $SessionId -Command $Command -TimeOut ($TimeoutSeconds * 1000)
        foreach ($line in @($fallback.Output)) {
            $text = [string]$line
            [void]$lines.Add($text)
            if ($text -match '^__PROGRESS__\|(\d{1,3})\|(.*)$') {
                $percent = [Math]::Min(100, [Math]::Max(0, [int]$Matches[1]))
                Write-Progress -Id 1 -Activity "Deploying to server" -Status $Matches[2] -PercentComplete $percent
                Write-Host $Matches[2] -ForegroundColor DarkCyan
            }
            elseif ($text) {
                Write-Host $text
            }
        }
        Write-Progress -Id 1 -Activity "Deploying to server" -Completed
        $remoteStatus = $fallback
    }

    return [pscustomobject]@{
        Output = @($lines)
        ExitStatus = [int]$remoteStatus.ExitStatus
        Error = @($remoteStatus.Error)
    }
}

function New-DeploymentImages {
    param(
        [string]$SavedStreamPath,
        [string]$TeleBoxPath,
        [string]$Platform,
        [string]$SavedStreamImage,
        [string]$TeleBoxImage,
        [string]$ImageTar,
        [string]$ImageArchive
    )

    Write-Host "Building Linux images locally for $Platform..." -ForegroundColor Cyan
    Write-Progress -Id 0 -Activity "Building Linux images" -Status "SavedStream image" -PercentComplete 10
    & docker build --platform $Platform --tag $SavedStreamImage $SavedStreamPath
    if ($LASTEXITCODE -ne 0) {
        throw "SavedStream image build failed with exit code $LASTEXITCODE."
    }

    Write-Progress -Id 0 -Activity "Building Linux images" -Status "TeleBox image" -PercentComplete 45
    & docker build --platform $Platform --file (Join-Path $TeleBoxPath "Dockerfile.bridge") --tag $TeleBoxImage $TeleBoxPath
    if ($LASTEXITCODE -ne 0) {
        throw "TeleBox image build failed with exit code $LASTEXITCODE."
    }

    Write-Host "Exporting and compressing deployment images..." -ForegroundColor Cyan
    Write-Progress -Id 0 -Activity "Building Linux images" -Status "Exporting and compressing images" -PercentComplete 80
    & docker image save --output $ImageTar $SavedStreamImage $TeleBoxImage
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image export failed with exit code $LASTEXITCODE."
    }

    Push-Location (Split-Path -Parent $ImageTar)
    try {
        & tar.exe --options gzip:compression-level=3 -czf $ImageArchive (Split-Path -Leaf $ImageTar)
        if ($LASTEXITCODE -ne 0) {
            throw "Image compression failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
    Write-Progress -Id 0 -Activity "Building Linux images" -Completed
}

$root = $PSScriptRoot
$savedStream = Join-Path $root "_src"
$teleBox = Join-Path $root "TeleBox"
$configPath = Join-Path $root "deploy.config.json"
$serverWasProvided = $PSBoundParameters.ContainsKey("Server")
$userWasProvided = $PSBoundParameters.ContainsKey("SshUser")
$domainWasProvided = $PSBoundParameters.ContainsKey("Domain")

if ($ResetConfig -and (Test-Path -LiteralPath $configPath)) {
    Remove-Item -LiteralPath $configPath -Force
    Write-Host "Saved deployment configuration removed." -ForegroundColor Yellow
}

if (-not (Test-Path (Join-Path $savedStream "docker-compose.yml"))) {
    throw "Missing $savedStream\docker-compose.yml"
}
if (-not (Test-Path (Join-Path $teleBox "Dockerfile.bridge"))) {
    throw "Missing $teleBox\Dockerfile.bridge"
}
if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
    throw "Windows tar.exe is required."
}

$stamp = Get-Date -Format "yyyyMMddHHmmss"
$archive = Join-Path $env:TEMP "tube-deploy-$stamp.tgz"
New-DeploymentArchive -Root $root -Archive $archive

if ($PackageOnly) {
    Write-Host "Package validation completed: $archive" -ForegroundColor Green
    & tar.exe -tzf $archive | Select-Object -First 20
    exit 0
}

$savedConfig = if ($ResetConfig) { $null } else { Read-DeploymentConfig -Path $configPath }
if ($savedConfig) {
    if (-not $serverWasProvided) { $Server = $savedConfig.Server }
    if (-not $userWasProvided) { $SshUser = $savedConfig.SshUser }
    if (-not $domainWasProvided) { $Domain = $savedConfig.Domain }
}

if (-not $Server) {
    $Server = Read-Host "Server IP or host"
}
if (-not $domainWasProvided -and -not $savedConfig) {
    $Domain = Read-Host "Domain (blank for IP over HTTP)"
}

Assert-Inputs

$buildLocally = $false
$usePrebuiltImages = $false
$imageArchiveIsTemporary = $true
$imageArchive = $null
$imageTarMember = $null
$prebuiltSavedStreamImage = $null
$prebuiltTeleBoxImage = $null
if ($PrebuiltImageArchive) {
    $imageArchive = (Resolve-Path -LiteralPath $PrebuiltImageArchive -ErrorAction Stop).Path
    $imageTarMember = Get-ImageTarMember -Archive $imageArchive
    $prebuiltTags = Get-PrebuiltImageTags -Archive $imageArchive -ImageTarMember $imageTarMember
    $prebuiltSavedStreamImage = $prebuiltTags.SavedStream
    $prebuiltTeleBoxImage = $prebuiltTags.TeleBox
    $usePrebuiltImages = $true
    $imageArchiveIsTemporary = $false
    Write-Host "Using prebuilt image archive: $imageArchive" -ForegroundColor Cyan
}
else {
    try {
        Assert-LocalDocker
        $buildLocally = $true
        $usePrebuiltImages = $true
    }
    catch {
        # Keep one-command deployment working when Docker Desktop is unavailable.
        # A prebuilt archive is faster, but server-side build remains the automatic
        # fallback for users who simply run deploy.ps1 without extra switches.
        Write-Warning "Local Docker Desktop is unavailable. Falling back to a server-side Docker build; this may take longer."
    }
}
Ensure-PoshSsh

$password = $null
if (
    $savedConfig -and
    $savedConfig.Server -eq $Server -and
    $savedConfig.SshUser -eq $SshUser
) {
    $password = $savedConfig.Password
    Write-Host "Using saved deployment configuration for $SshUser@$Server." -ForegroundColor Cyan
}
if (-not $password) {
    $password = Read-Host "SSH password" -AsSecureString
}
$credential = New-Object System.Management.Automation.PSCredential($SshUser, $password)
$remoteArchive = "/tmp/tube-deploy-$stamp.tgz"
$imageTar = Join-Path $env:TEMP "tube-images-$stamp.tar"
if (-not $imageArchive) {
    $imageArchive = Join-Path $env:TEMP "tube-images-$stamp.tgz"
}
$remoteImageArchive = "/tmp/$([IO.Path]::GetFileName($imageArchive))"
if ($remoteImageArchive -notmatch '^/tmp/[A-Za-z0-9._-]+$') {
    throw "Image archive file name must contain only letters, digits, dots, underscores, or hyphens."
}
$savedStreamImage = "savedstream:deploy-$stamp"
$teleBoxImage = "telebox-bridge:deploy-$stamp"
$adminCandidate = New-HexSecret
$apiTokenCandidate = New-HexSecret
$secretKeyCandidate = New-HexSecret
$mediaCacheKeyCandidate = New-HexSecret
$site = if ($Domain) { $Domain.ToLowerInvariant() } else { "http://$Server" }
$cookieSecure = if ($Domain) { "true" } else { "false" }

$ssh = $null
$sftp = $null
try {
    Write-Host "Connecting to $SshUser@$Server..." -ForegroundColor Cyan
    $ssh = New-SSHSession -ComputerName $Server -Credential $credential -AcceptKey -ConnectionTimeout 30
    $identity = Invoke-SSHCommand -SessionId $ssh.SessionId -Command "id -u" -TimeOut 30000
    if (($identity.Output | Select-Object -First 1).Trim() -ne "0") {
        throw "This deployment script currently requires the root SSH user."
    }
    try {
        Save-DeploymentConfig -Path $configPath -Server $Server -SshUser $SshUser -Domain $Domain -Password $password
        Write-Host "Deployment configuration saved to $configPath" -ForegroundColor DarkGray
    }
    catch {
        Write-Warning "Deployment configuration could not be saved: $($_.Exception.Message)"
    }

    $architectureResult = Invoke-SSHCommand -SessionId $ssh.SessionId -Command "uname -m" -TimeOut 30000
    $architecture = ($architectureResult.Output | Select-Object -First 1).Trim().ToLowerInvariant()
    $platform = switch ($architecture) {
        { $_ -in @("x86_64", "amd64") } { "linux/amd64"; break }
        { $_ -in @("aarch64", "arm64") } { "linux/arm64"; break }
        default { throw "Unsupported server architecture: $architecture" }
    }

    if ($buildLocally) {
        New-DeploymentImages `
            -SavedStreamPath $savedStream `
            -TeleBoxPath $teleBox `
            -Platform $platform `
            -SavedStreamImage $savedStreamImage `
            -TeleBoxImage $teleBoxImage `
            -ImageTar $imageTar `
            -ImageArchive $imageArchive
        $imageTarMember = Split-Path -Leaf $imageTar
        $prebuiltSavedStreamImage = $savedStreamImage
        $prebuiltTeleBoxImage = $teleBoxImage
    }
    elseif (-not $usePrebuiltImages) {
        Write-Host "No local/prebuilt images; source will be built on the server." -ForegroundColor Yellow
    }

    $sftp = New-SFTPSession -ComputerName $Server -Credential $credential -AcceptKey -ConnectionTimeout 30
    Write-Host "Uploading source and prebuilt images..." -ForegroundColor Cyan
    Write-Progress -Id 0 -Activity "Uploading deployment artifacts" -Status "Uploading source archive" -PercentComplete 10
    Set-SFTPItem -SessionId $sftp.SessionId -Path $archive -Destination "/tmp" -Force
    if ($usePrebuiltImages) {
        Write-Progress -Id 0 -Activity "Uploading deployment artifacts" -Status "Uploading image archive" -PercentComplete 55
        Set-SFTPItem -SessionId $sftp.SessionId -Path $imageArchive -Destination "/tmp" -Force
    }
    Write-Progress -Id 0 -Activity "Uploading deployment artifacts" -Completed

    $remoteTemplate = @'
set -Eeuo pipefail

BASE=/opt/tube
STAMP='__STAMP__'
ARCHIVE='__ARCHIVE__'
IMAGE_ARCHIVE='__IMAGE_ARCHIVE__'
IMAGE_TAR_NAME='__IMAGE_TAR_NAME__'
BUILD_MODE='__BUILD_MODE__'
STAGING="$BASE/.staging-$STAMP"
CODE_BACKUP="$BASE/backups/code-$STAMP"
VOLUME_BACKUP="$BASE/backups/volumes-$STAMP"
SITE='__SITE__'
COOKIE_SECURE='__COOKIE_SECURE__'
ADMIN_CANDIDATE='__ADMIN_CANDIDATE__'
API_TOKEN_CANDIDATE='__API_TOKEN_CANDIDATE__'
SECRET_KEY_CANDIDATE='__SECRET_KEY_CANDIDATE__'
MEDIA_CACHE_KEY_CANDIDATE='__MEDIA_CACHE_KEY_CANDIDATE__'
SAVEDSTREAM_IMAGE='__SAVEDSTREAM_IMAGE__'
TELEBOX_IMAGE='__TELEBOX_IMAGE__'
PREBUILT_SAVEDSTREAM_IMAGE='__PREBUILT_SAVEDSTREAM_IMAGE__'
PREBUILT_TELEBOX_IMAGE='__PREBUILT_TELEBOX_IMAGE__'

cleanup() {
  rm -f "$ARCHIVE"
  rm -f "$IMAGE_ARCHIVE"
  rm -rf "$STAGING"
}
trap cleanup EXIT

progress() {
  printf '__PROGRESS__|%s|%s\n' "$1" "$2"
}

command -v docker >/dev/null || { echo 'Docker is not installed.'; exit 1; }
docker compose version >/dev/null || { echo 'Docker Compose v2 is not available.'; exit 1; }
command -v curl >/dev/null || { echo 'curl is not installed.'; exit 1; }
command -v base64 >/dev/null || { echo 'base64 is not installed.'; exit 1; }

mkdir -p "$BASE/backups"
# The SavedStream container (UID 10001) needs write access to remove old
# backups from the admin console; keep other host users out of the backups.
chown 10001:10001 "$BASE/backups" 2>/dev/null || true
chmod 700 "$BASE/backups" 2>/dev/null || true
rm -rf "$STAGING"
mkdir -p "$STAGING"
tar xzf "$ARCHIVE" -C "$STAGING"
test -f "$STAGING/_src/docker-compose.yml"
test -f "$STAGING/TeleBox/Dockerfile.bridge"
progress 15 'Source archive extracted'
if [ "$BUILD_MODE" = local ]; then
  tar tzf "$IMAGE_ARCHIVE" | grep -Fxq "$IMAGE_TAR_NAME"
fi

ENV_FILE="$STAGING/_src/.env"
if [ -f "$BASE/_src/.env" ]; then
  cp "$BASE/_src/.env" "$ENV_FILE"
else
  : > "$ENV_FILE"
fi

ensure_env() {
  key="$1"
  value="$2"
  if ! grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

ensure_secret() {
  key="$1"
  value="$2"
  if ! grep -Eq "^${key}=.+" "$ENV_FILE"; then
    sed -i "/^${key}=/d" "$ENV_FILE"
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

set_env() {
  key="$1"
  value="$2"
  sed -i "/^${key}=/d" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}

ensure_secret ADMIN_KEY "$ADMIN_CANDIDATE"
ensure_secret TELEBOX_API_TOKEN "$API_TOKEN_CANDIDATE"
ensure_secret TELEBOX_SECRET_KEY "$SECRET_KEY_CANDIDATE"
ensure_secret MEDIA_CACHE_KEY "$MEDIA_CACHE_KEY_CANDIDATE"
ensure_env TELEBOX_DEFAULT_ACCOUNT default
ensure_env SESSION_COOKIE_DAYS 30
set_env PORT 8000
set_env COOKIE_SECURE "$COOKIE_SECURE"
set_env CADDY_SITE "$SITE"
set_env SAVEDSTREAM_IMAGE "$SAVEDSTREAM_IMAGE"
set_env TELEBOX_IMAGE "$TELEBOX_IMAGE"
chmod 600 "$ENV_FILE"
progress 20 'Deployment environment prepared'

cat > "$STAGING/_src/Caddyfile" <<EOF
$SITE {
    encode zstd gzip
    reverse_proxy savedstream:8000
}
EOF

if docker ps -a --filter label=com.docker.compose.project=_src -q | grep -q .; then
  PROJECT=_src
elif docker ps -a --filter label=com.docker.compose.project=savedstream -q | grep -q .; then
  PROJECT=savedstream
else
  PROJECT=savedstream
fi

MANAGE_CADDY=1
PORTS_BUSY=0
if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -Eq ':(80|443)[[:space:]]'; then
  PORTS_BUSY=1
elif command -v netstat >/dev/null && netstat -ltn 2>/dev/null | grep -Eq ':(80|443)[[:space:]]'; then
  PORTS_BUSY=1
fi

OWN_CADDY=$(docker ps \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter 'label=com.docker.compose.service=caddy' \
  -q | head -n 1)
if [ "$PORTS_BUSY" = 1 ] && [ -z "$OWN_CADDY" ]; then
  MANAGE_CADDY=0
  echo 'Port 80 or 443 is managed by an external service; bundled Caddy will be skipped.'
fi

echo "Compose project: $PROJECT"
progress 25 'Preparing application images'
docker image inspect alpine:3.20 >/dev/null 2>&1 || docker pull alpine:3.20
if [ "$BUILD_MODE" = local ]; then
  echo 'Loading locally built application images...'
  tar -xOzf "$IMAGE_ARCHIVE" "$IMAGE_TAR_NAME" | docker load
  if [ -n "$PREBUILT_SAVEDSTREAM_IMAGE" ] && [ "$PREBUILT_SAVEDSTREAM_IMAGE" != "$SAVEDSTREAM_IMAGE" ]; then
    docker image tag "$PREBUILT_SAVEDSTREAM_IMAGE" "$SAVEDSTREAM_IMAGE"
  fi
  if [ -n "$PREBUILT_TELEBOX_IMAGE" ] && [ "$PREBUILT_TELEBOX_IMAGE" != "$TELEBOX_IMAGE" ]; then
    docker image tag "$PREBUILT_TELEBOX_IMAGE" "$TELEBOX_IMAGE"
  fi
  progress 45 'Prebuilt application images loaded'
else
  echo 'Building application images on the server...'
  docker compose -p "$PROJECT" -f "$STAGING/_src/docker-compose.yml" --project-directory "$STAGING/_src" build
  progress 45 'Application images built on server'
fi
docker compose -p "$PROJECT" -f "$STAGING/_src/docker-compose.yml" --project-directory "$STAGING/_src" config >/dev/null
progress 50 'Compose configuration validated'
if [ "$MANAGE_CADDY" = 1 ]; then
  docker compose -p "$PROJECT" -f "$STAGING/_src/docker-compose.yml" --project-directory "$STAGING/_src" pull caddy
fi
progress 55 'Service images ready'

OLD_COMPOSE="$BASE/_src/docker-compose.yml"
if [ -f "$OLD_COMPOSE" ]; then
  docker compose -p "$PROJECT" -f "$OLD_COMPOSE" --project-directory "$BASE/_src" stop || true
fi
progress 58 'Previous deployment stopped; backing up persistent data'

mkdir -p "$VOLUME_BACKUP"
backup_volume() {
  suffix="$1"
  volume="$2"
  percent="$3"
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Backing up volume $volume"
    docker run --rm -v "$volume:/data:ro" -v "$VOLUME_BACKUP:/backup" alpine:3.20 sh -c "tar czf /backup/$suffix.tgz -C /data ."
    progress "$percent" "Volume $volume backed up"
  fi
}

backup_pids=()
backup_index=0
for suffix in savedstream-data telebox-data caddy-data caddy-config; do
  volume="${PROJECT}_${suffix}"
  backup_index=$((backup_index + 1))
  backup_volume "$suffix" "$volume" $((60 + backup_index * 4)) &
  backup_pids+=("$!")
done
backup_failed=0
for pid in "${backup_pids[@]}"; do
  if ! wait "$pid"; then backup_failed=1; fi
done
[ "$backup_failed" = 0 ] || { echo 'Volume backup failed.'; exit 1; }
progress 76 'Persistent data backups completed'

mkdir -p "$CODE_BACKUP"
if [ -d "$BASE/_src" ]; then mv "$BASE/_src" "$CODE_BACKUP/_src"; fi
if [ -d "$BASE/TeleBox" ]; then mv "$BASE/TeleBox" "$CODE_BACKUP/TeleBox"; fi
mv "$STAGING/_src" "$BASE/_src"
mv "$STAGING/TeleBox" "$BASE/TeleBox"
rmdir "$STAGING" 2>/dev/null || true
progress 80 'Code backup created and new release activated'

rollback() {
  echo 'New version failed health checks. Rolling code back...'
  docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" down || true
  for suffix in savedstream-data telebox-data caddy-data caddy-config; do
    volume="${PROJECT}_${suffix}"
    backup="$VOLUME_BACKUP/$suffix.tgz"
    if [ -f "$backup" ] && docker volume inspect "$volume" >/dev/null 2>&1; then
      echo "Restoring volume $volume"
      docker run --rm -v "$volume:/data" -v "$VOLUME_BACKUP:/backup:ro" alpine:3.20 \
        sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf /backup/$suffix.tgz -C /data"
    fi
  done
  mkdir -p "$BASE/failed-$STAMP"
  mv "$BASE/_src" "$BASE/failed-$STAMP/_src" || true
  mv "$BASE/TeleBox" "$BASE/failed-$STAMP/TeleBox" || true
  if [ -d "$CODE_BACKUP/_src" ]; then mv "$CODE_BACKUP/_src" "$BASE/_src"; fi
  if [ -d "$CODE_BACKUP/TeleBox" ]; then mv "$CODE_BACKUP/TeleBox" "$BASE/TeleBox"; fi
  if [ -f "$BASE/_src/docker-compose.yml" ]; then
    if [ "$MANAGE_CADDY" = 1 ]; then
      docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" up -d
    else
      docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" up -d savedstream
    fi
  fi
  exit 1
}

if [ "$MANAGE_CADDY" = 1 ]; then
  progress 84 'Starting updated containers'
  if ! docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" up -d --remove-orphans; then
    docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" logs --tail=200 || true
    rollback
  fi
else
  progress 84 'Starting updated containers'
  if ! docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" up -d --remove-orphans savedstream; then
    docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" logs --tail=200 || true
    rollback
  fi
fi

healthy=0
for ((attempt=1; attempt<=60; attempt++)); do
  if (( attempt == 1 || attempt % 5 == 0 )); then
    progress $((86 + attempt / 10)) "Waiting for health check ($attempt/60)"
  fi
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [ "$healthy" != 1 ]; then
  docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" logs --tail=120 || true
  rollback
fi
progress 96 'Health check passed; pruning old backups'

# Keep only the newest __KEEP_BACKUPS__ deployment backups.  Each backup is
# a code-<stamp> + volumes-<stamp> pair; both are removed together.  The
# admin console can also manage these backups manually.
KEEP_BACKUPS='__KEEP_BACKUPS__'
if [ "$KEEP_BACKUPS" -gt 0 ] 2>/dev/null; then
  ls -1dt "$BASE"/backups/code-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r dir; do
    stamp="${dir##*/}"
    stamp="${stamp#code-}"
    if [ -n "$stamp" ] && [ "$stamp" != "code-*" ]; then
      echo "Pruning old backup: $stamp"
      rm -rf "$BASE/backups/code-$stamp" "$BASE/backups/volumes-$stamp"
    fi
  done
  # Remove orphaned volume backups left by interrupted deployments.
  for dir in "$BASE"/backups/volumes-*; do
    [ -d "$dir" ] || continue
    stamp="${dir##*/}"
    stamp="${stamp#volumes-}"
    [ -d "$BASE/backups/code-$stamp" ] || rm -rf "$dir"
  done
fi
progress 99 'Old backups pruned (retaining at most __KEEP_BACKUPS__)'

if [ "$MANAGE_CADDY" = 1 ] && command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

ADMIN_KEY=$(sed -n 's/^ADMIN_KEY=//p' "$BASE/_src/.env" | tail -n 1)
echo "__ADMIN_KEY__=$ADMIN_KEY"
echo "__SITE__=$SITE"
echo "__EXTERNAL_CADDY__=$((1 - MANAGE_CADDY))"
echo '__DEPLOY_OK__=1'
progress 100 'Deployment completed successfully'
docker compose -p "$PROJECT" -f "$BASE/_src/docker-compose.yml" --project-directory "$BASE/_src" ps
'@

    $remoteScript = $remoteTemplate.
        Replace('__STAMP__', $stamp).
        Replace('__ARCHIVE__', $remoteArchive).
        Replace('__IMAGE_ARCHIVE__', $remoteImageArchive).
        Replace('__IMAGE_TAR_NAME__', [string]$imageTarMember).
        Replace('__BUILD_MODE__', $(if ($usePrebuiltImages) { 'local' } else { 'server' })).
        Replace('__SITE__', $site).
        Replace('__COOKIE_SECURE__', $cookieSecure).
        Replace('__ADMIN_CANDIDATE__', $adminCandidate).
        Replace('__API_TOKEN_CANDIDATE__', $apiTokenCandidate).
        Replace('__SECRET_KEY_CANDIDATE__', $secretKeyCandidate).
        Replace('__MEDIA_CACHE_KEY_CANDIDATE__', $mediaCacheKeyCandidate).
        Replace('__SAVEDSTREAM_IMAGE__', $savedStreamImage).
        Replace('__TELEBOX_IMAGE__', $teleBoxImage).
        Replace('__PREBUILT_SAVEDSTREAM_IMAGE__', [string]$prebuiltSavedStreamImage).
        Replace('__PREBUILT_TELEBOX_IMAGE__', [string]$prebuiltTeleBoxImage).
        Replace('__KEEP_BACKUPS__', $KeepBackups)

    $remoteScript = $remoteScript.Replace("`r`n", "`n")
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
    Write-Host "Building, backing up, and deploying..." -ForegroundColor Cyan
    $result = Invoke-RemoteDeployment -SessionId $ssh.SessionId -Command "echo '$encoded' | base64 -d | bash" -TimeoutSeconds 3600
    $output = @($result.Output)
    if ($result.ExitStatus -ne 0) {
        if ($result.Error) {
            $result.Error | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        }
        throw "Remote deployment failed with exit code $($result.ExitStatus)."
    }

    $adminLine = $output | Where-Object { $_ -like "__ADMIN_KEY__=*" } | Select-Object -Last 1
    $siteLine = $output | Where-Object { $_ -like "__SITE__=*" } | Select-Object -Last 1
    $externalCaddy = $output -contains "__EXTERNAL_CADDY__=1"
    if (-not $adminLine) {
        throw "Deployment succeeded but ADMIN_KEY was not returned. Check /opt/tube/_src/.env."
    }

    $adminKey = $adminLine.Substring("__ADMIN_KEY__=".Length)
    $deployedSite = if ($siteLine) { $siteLine.Substring("__SITE__=".Length) } else { $site }
    $adminUrl = if ($deployedSite -match '^https?://') { "$deployedSite/admin" } else { "https://$deployedSite/admin" }

    Write-Host ""
    Write-Host "Deployment completed." -ForegroundColor Green
    Write-Host "Admin URL: $adminUrl" -ForegroundColor Cyan
    Write-Host "ADMIN_KEY: $adminKey" -ForegroundColor Yellow
    Write-Host "Code backup: /opt/tube/backups/code-$stamp"
    Write-Host "Volume backup: /opt/tube/backups/volumes-$stamp"
    if ($externalCaddy) {
        Write-Host ""
        Write-Host "Ports 80/443 are managed externally. Add this to the existing Caddyfile:" -ForegroundColor Yellow
        Write-Host "$deployedSite {"
        Write-Host "    encode zstd gzip"
        Write-Host "    reverse_proxy 127.0.0.1:8000"
        Write-Host "}"
        Write-Host "Then reload Caddy, for example: caddy reload --config /etc/caddy/Caddyfile"
    }
}
finally {
    if ($sftp) {
        Remove-SFTPSession -SessionId $sftp.SessionId | Out-Null
    }
    if ($ssh) {
        Remove-SSHSession -SessionId $ssh.SessionId | Out-Null
    }
    if (Test-Path $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    if (Test-Path $imageTar) {
        Remove-Item -LiteralPath $imageTar -Force
    }
    if ($imageArchiveIsTemporary -and (Test-Path $imageArchive)) {
        Remove-Item -LiteralPath $imageArchive -Force
    }
}
