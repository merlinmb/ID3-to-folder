# PowerShell script to launch the organiser remotely via SSH
# Usage: .\launch.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remoteDir  = "/home/merlin/bin/id3"
$sourceDir  = "/mnt/media2/toprocess"
$processed  = "/mnt/media2/processed"
$unmatched  = "/mnt/media2/unprocessed"

$organiseCmd = "venv/bin/python id3_organiser.py organise $sourceDir --processed $processed --unmatched $unmatched"
ssh ${remoteUser}@${remoteHost} "cd $remoteDir; $organiseCmd"
