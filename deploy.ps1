# PowerShell script to deploy project to remote server and (re)start Docker container
# Usage: .\deploy.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remotePath = "/home/merlin/bin/id3"

# Package files (does NOT include .venv, tests, docs, .git, data/)
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

# Compress (from inside tempDir to keep clean relative paths in zip)
$zipFile = (Resolve-Path ".").Path + "\deploy_package.zip"
if (Test-Path $zipFile) { Remove-Item $zipFile -Force }
Push-Location $tempDir
Compress-Archive -Path * -DestinationPath $zipFile
Pop-Location

# Transfer
Write-Host "Transferring package to ${remoteHost}..."
scp $zipFile "${remoteUser}@${remoteHost}:/tmp/"

# Deploy on remote:
#   - mkdir -p preserves the data/ directory (and its SQLite DB) across deploys
#   - docker compose up --build -d rebuilds the image and restarts the container
$sshCmd = @"
set -ex
mkdir -p $remotePath
python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/deploy_package.zip'); [setattr(m,'filename',m.filename.replace('\\\\','/')) or z.extract(m,'$remotePath') for m in z.infolist()]"
rm -f /tmp/deploy_package.zip
cd $remotePath
docker compose up --build -d
docker compose ps
"@

Write-Host "Deploying on remote server..."
$remoteScript = "deploy_remote.sh"
[System.IO.File]::WriteAllText($remoteScript, ($sshCmd -replace "\r", ""))
scp $remoteScript "${remoteUser}@${remoteHost}:/tmp/deploy_remote.sh"
ssh ${remoteUser}@${remoteHost} "bash /tmp/deploy_remote.sh && rm /tmp/deploy_remote.sh"
Remove-Item $remoteScript

# Clean up local temp files
Remove-Item $zipFile -Force
Remove-Item $tempDir -Recurse -Force

Write-Host "Deployment complete."
