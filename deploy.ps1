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
"	rm -rf $remotePath
"	mkdir -p $remotePath
"	unzip -o /tmp/deploy_package.zip -d $remotePath
"	rm /tmp/deploy_package.zip
"	cd $remotePath
"	sudo apt-get install -y python3-venv 2>&1 | tail -1
"	python3 -m venv .venv
"	.venv/bin/pip install --upgrade pip
"	.venv/bin/pip install -r requirements.txt
"@
ssh ${remoteUser}@${remoteHost} ($sshCmd -replace "\r", "")

# Clean up
Remove-Item $zipFile -Force
Remove-Item $tempDir -Recurse -Force

Write-Host "Deployment complete."
