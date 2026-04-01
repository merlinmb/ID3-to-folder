# PowerShell script to deploy project to remote server and (re)start Docker container
# Builds the Docker image locally and transfers it — no internet access needed on the server.
# Usage: .\deploy.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remotePath = "/home/merlin/bin/id3"
$imageName  = "id3-organiser"
$imageTar   = "id3-organiser.tar"

# ── 1. Build Docker image locally ────────────────────────────────────────────
Write-Host "Building Docker image..."
docker build -t $imageName .
if ($LASTEXITCODE -ne 0) { Write-Error "Docker build failed"; exit 1 }

# ── 2. Save image to tar ──────────────────────────────────────────────────────
Write-Host "Saving image to $imageTar..."
docker save $imageName -o $imageTar
if ($LASTEXITCODE -ne 0) { Write-Error "Docker save failed"; exit 1 }

# ── 3. Package app files ──────────────────────────────────────────────────────
$tempDir = "deploy_temp"
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
New-Item -ItemType Directory -Path $tempDir | Out-Null

Copy-Item id3_organiser.py      $tempDir/
Copy-Item requirements.txt      $tempDir/
Copy-Item Dockerfile            $tempDir/
Copy-Item docker-compose.yml    $tempDir/
Copy-Item .env                  $tempDir/
if (Test-Path "templates")  { Copy-Item templates  $tempDir/ -Recurse }
if (Test-Path "enrichment") { Copy-Item enrichment $tempDir/ -Recurse }
Get-ChildItem $tempDir -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force

$zipFile = (Resolve-Path ".").Path + "\deploy_package.zip"
if (Test-Path $zipFile) { Remove-Item $zipFile -Force }
Push-Location $tempDir
Compress-Archive -Path * -DestinationPath $zipFile
Pop-Location

# ── 4. Transfer image + package ───────────────────────────────────────────────
Write-Host "Transferring to ${remoteHost}..."
scp $imageTar   "${remoteUser}@${remoteHost}:/tmp/"
scp $zipFile    "${remoteUser}@${remoteHost}:/tmp/"

# ── 5. Deploy on remote ───────────────────────────────────────────────────────
# docker compose down stops and removes the container but NOT bind-mount volumes,
# so data/ (SQLite DB) is preserved across deploys.
$sshCmd = @"
set -ex
mkdir -p $remotePath
python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/deploy_package.zip'); [setattr(m,'filename',m.filename.replace('\\\\','/')) or z.extract(m,'$remotePath') for m in z.infolist()]"
rm -f /tmp/deploy_package.zip
cd $remotePath
docker compose down
docker load -i /tmp/$imageTar
rm -f /tmp/$imageTar
docker compose up -d
docker compose ps
"@

Write-Host "Deploying on remote server..."
$remoteScript = "deploy_remote.sh"
[System.IO.File]::WriteAllText($remoteScript, ($sshCmd -replace "\r", ""))
scp $remoteScript "${remoteUser}@${remoteHost}:/tmp/deploy_remote.sh"
ssh ${remoteUser}@${remoteHost} "bash /tmp/deploy_remote.sh && rm /tmp/deploy_remote.sh"
Remove-Item $remoteScript

# ── 6. Clean up local temp files ──────────────────────────────────────────────
Remove-Item $imageTar -Force
Remove-Item $zipFile  -Force
Remove-Item $tempDir  -Recurse -Force

Write-Host "Deployment complete."
