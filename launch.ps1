# PowerShell script to launch the organiser remotely via SSH
# Usage: .\launch.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remoteDir  = "/home/merlin/bin/id3"
$venvActivate = "source .venv/bin/activate"
$launchCmd = "venv/bin/python id3_organiser.py organise /mnt/media2/toprocess --processed /mnt/media2/processed --unmatched /mnt/media2/unprocessed"

$sshCmd = "cd $remoteDir && $launchCmd"
ssh ${remoteUser}@${remoteHost} $sshCmd
