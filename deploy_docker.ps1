# PowerShell script to deploy the organiser to a Docker container via SSH
# Usage: .\deploy_docker.ps1

$remoteUser = "merlin"
$remoteHost = "192.168.1.50"
$remoteDir  = "/home/merlin/bin/id3"
$containerName = "id3_organiser"
$imageName = "id3_organiser:latest"

# Paths to map (local:remote)
$localToprocess = "/mnt/media2/toprocess"
$localProcessed = "/mnt/media2/processed"
$localUnmatched = "/mnt/media2/unprocessed"

# Copy project files and .env to remote
scp -r * .env ${remoteUser}@${remoteHost}:$remoteDir


# Build and run Docker container on remote
$dockerCmd = "cd /home/merlin/bin/id3; " +
	"docker build -t id3_organiser:latest .; " +
	"docker rm -f id3_organiser 2>/dev/null || true; " +
	"docker run -d --name id3_organiser " +
	"--env-file .env " +
	"-v /mnt/media2/toprocess:/mnt/media2/toprocess " +
	"-v /mnt/media2/processed:/mnt/media2/processed " +
	"-v /mnt/media2/unprocessed:/mnt/media2/unprocessed " +
	"id3_organiser:latest " +
	" python3 id3_organiser.py /mnt/media2/toprocess --processed /mnt/media2/processed --unmatched /mnt/media2/unprocessed";

ssh ${remoteUser}@${remoteHost} $dockerCmd
