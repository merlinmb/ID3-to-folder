# PowerShell Deploy Script for ID3-to-folder
# Usage: .\deploy.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remoteDir = "/home/merlin/bin/id3"
$venvDir = "$remoteDir/venv"

# Rsync equivalent in PowerShell: use scp for files, or use WinSCP/pscp for directories
# We'll use scp for simplicity, but recommend WinSCP for large projects

# Exclude venv, __pycache__, .git, etc.
$excludeDirs = @("venv", ".venv", "__pycache__", ".git", ".pytest_cache", ".mypy_cache", "docs", "tests")

# Create a temporary zip file excluding unwanted folders/files
$zipPath = "$env:TEMP\id3_deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

# Exclude any file whose full path contains an excluded directory segment
$items = Get-ChildItem -Recurse -File | Where-Object {
    $filePath = $_.FullName
    $inExcluded = $false
    foreach ($dir in $excludeDirs) {
        if ($filePath -match "([\\\/])$([regex]::Escape($dir))([\\\/]|$)") {
            $inExcluded = $true
            break
        }
    }
    -not $inExcluded -and $_.Name -ne 'deploy.ps1' -and $_.Name -ne 'deploy_docker.ps1' -and $_.Name -ne 'settings.local.json'
}
if ($items.Count -eq 0) {
    Write-Error "No files to archive."
    exit 1
}
Write-Host "Archiving $($items.Count) files..."

# Stage files into a temp directory preserving relative paths, then zip
$stageDir = "$env:TEMP\id3_stage"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Path $stageDir | Out-Null
$root = (Get-Location).Path
$items | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    Write-Host "  $rel"
    $dest = Join-Path $stageDir $rel
    $destDir = Split-Path $dest -Parent
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir | Out-Null }
    Copy-Item $_.FullName $dest
}
Compress-Archive -Path "$stageDir\*" -DestinationPath $zipPath

# Copy zip to remote
Write-Host "Uploading files..."
scp $zipPath "${remoteUser}@${remoteHost}:${remoteDir}/id3_deploy.zip"

# SSH to remote: clean old files, unzip, create venv if needed, install requirements from PyPI
$sshCmd = "cd $remoteDir && find . -maxdepth 1 -not -name venv -not -name '.' -not -name 'id3_deploy.zip' -exec rm -rf {} + && unzip -o id3_deploy.zip -d . && rm -f id3_deploy.zip && if [ ! -d venv ]; then python3 -m venv venv; fi && venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.txt"
ssh $remoteUser@$remoteHost $sshCmd

Write-Host "Deployment complete."
