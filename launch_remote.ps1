# PowerShell script to launch id3_organiser.py on remote server
# Usage: .\launch_remote.ps1
# Ctrl+C the Python script to drop into an interactive SSH session

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remotePath = "/home/merlin/bin/id3"

Write-Host "Connecting to ${remoteUser}@${remoteHost}..."
Write-Host "Working directory: $remotePath"
Write-Host "Activating venv and launching id3_organiser.py..."
Write-Host "(Ctrl+C will stop the script and leave you in an interactive shell)"
Write-Host ""

# -t forces a pseudo-terminal so the session stays interactive after the script exits
# Uses full venv python path directly to avoid 'source' issues in non-interactive SSH shells
# Sources activate before 'exec bash' so the drop-in shell inherits the venv environment
ssh -t ${remoteUser}@${remoteHost} "bash -c 'cd $remotePath && $remotePath/bin/python3 $remotePath/id3_organiser.py /mnt/media2/toprocess --processed /mnt/media2/processed --unmatched /mnt/media2/unprocessed; source $remotePath/bin/activate; exec bash'"
