# ID3 Music Organiser

A Python tool to scan, organize, and manage your music library using ID3 and audio metadata tags. Provides a web UI for reviewing and triggering file moves.

## Features
- Scans directories for supported music files (.mp3, .flac, .aac, .m4a)
- Extracts metadata and organizes files into artist/album folders
- Handles unmatched files separately
- Web interface for reviewing, editing, and moving tracks
- Progress reporting and error handling

## Requirements
- Python 3.8+
- See `requirements.txt` for Python dependencies

## Installation
1. Clone the repository:
   ```sh
   git clone https://github.com/merlinmb/ID3-to-folder.git
   cd ID3-to-folder
   ```
2. Create a virtual environment and install dependencies:
   ```sh
   python -m venv .venv
   .venv/Scripts/activate  # On Windows
   source .venv/bin/activate  # On Linux/macOS
   pip install -r requirements.txt
   ```

## Usage
Run the organiser from the command line:

```sh
python id3_organiser.py /path/to/music
```

Optional arguments:
- `--db`        Path to SQLite database file (default: `music_organiser.db`)
- `--processed` Output directory for organized files (default: `./processed`)
- `--unmatched` Output directory for unmatched files (default: `./unmatched`)
- `--port`      Web server port (default: 5000)
- `--no-browser`  Do not auto-open the web UI in a browser

Example:
```sh
python id3_organiser.py /music --processed /output/processed --unmatched /output/unmatched
```

## Web Interface
After scanning, a web UI will be available (default: http://localhost:5000) for reviewing and editing track destinations, and triggering file moves.

## Deployment
A PowerShell script (`deploy.ps1`) is provided to deploy the project to a remote Linux server via SSH. It packages the project, transfers it, and sets up a Python virtual environment on the remote host.

## License
MIT License
