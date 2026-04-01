# PowerShell script to deploy project to remote server
# Usage: .\deploy.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remotePath = "/home/merlin/bin/id3"

# Create a temporary directory for packaging
$tempDir = "deploy_temp"
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
New-Item -ItemType Directory -Path $tempDir | Out-Null

Copy-Item id3_organiser.py $tempDir/
Copy-Item requirements.txt $tempDir/
if (Test-Path "templates") { Copy-Item templates $tempDir/ -Recurse }
if (Test-Path "enrichment") { Copy-Item enrichment $tempDir/ -Recurse }
Get-ChildItem $tempDir -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force

# Compress files for transfer (compress from inside tempDir to avoid backslash paths in zip)
$zipFile = (Resolve-Path ".").Path + "\deploy_package.zip"
if (Test-Path $zipFile) { Remove-Item $zipFile -Force }
Push-Location $tempDir
Compress-Archive -Path * -DestinationPath $zipFile
Pop-Location

# Transfer to remote server
Write-Host "Transferring package to remote server..."
scp $zipFile "${remoteUser}@${remoteHost}:/tmp/"

# SSH and deploy on remote
Write-Host "Deploying on remote server..."
$sshCmd = @"
set -ex
rm -rf $remotePath
mkdir -p $remotePath
python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/deploy_package.zip'); [setattr(m,'filename',m.filename.replace('\\\\','/')) or z.extract(m,'$remotePath') for m in z.infolist()]"
rm -f /tmp/deploy_package.zip
sudo apt-get install -y python3-venv python3-full 2>/dev/null || true
python3 -m venv $remotePath
source $remotePath/bin/activate
pip3 install --upgrade pip
pip3 install -r $remotePath/requirements.txt
"@

# Write remote script with Unix line endings to avoid \r in paths
$remoteScript = "deploy_remote.sh"
[System.IO.File]::WriteAllText($remoteScript, ($sshCmd -replace "\r", ""))
scp $remoteScript "${remoteUser}@${remoteHost}:/tmp/deploy_remote.sh"
ssh ${remoteUser}@${remoteHost} "bash /tmp/deploy_remote.sh && rm /tmp/deploy_remote.sh"
Remove-Item $remoteScript

# Clean up
Remove-Item $zipFile -Force
Remove-Item $tempDir -Recurse -Force

Write-Host "Deployment complete."
