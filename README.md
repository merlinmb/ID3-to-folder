# ID3 Music Organiser

A self-hosted music library organiser. Scans a source folder of audio files, reads ID3/metadata tags, and organises them into a structured `Artist/Album/Track` folder tree. Includes a three-stage enrichment pipeline (heuristics → MusicBrainz → Claude AI) to recover missing tags, and a web UI for reviewing destinations and triggering moves.

<img width="1911" height="721" alt="image" src="https://github.com/user-attachments/assets/f134c3ec-d2b4-41b3-ac63-f57f4e5c8355" />

---

## Features

- Scans `/input` for `.mp3`, `.flac`, `.aac`, `.m4a` files on startup
- Classifies tracks as **matched** (has artist + album + title) or **unmatched**
- Destination format: `/destination/<Artist>/<Album>/<NN>. <Title>.ext`; unmatched go to `/destination/_unmatched/<original-relative-path>`
- **Three-stage enrichment pipeline** for tracks with missing tags:
  1. **Heuristics** — folder structure / filename patterns (runs at startup, auto-applies high-confidence results)
  2. **MusicBrainz** — free API, no key required (daily at 02:00, up to 500 tracks/day)
  3. **Claude AI** — Anthropic batch API fallback (same daily run, up to 100 tracks/day)
- **Web UI** — review and edit tag destinations, approve/reject enrichment candidates, trigger moves
- **Inline tag editing** — edit artist, album, track number, or title directly in the tracks table; × button to clear a field immediately
- **CSV export** of track list with active filters applied
- **Directory watcher** — picks up new files dropped into `/input` without restart (polling mode for NAS mounts)
- Deployed as a Docker container; SQLite DB persists across redeploys via bind mount

---

## Quick Start (Docker — recommended)

1. Copy `.env.example` to `.env` and set your paths:
   ```
   SOURCE_PATH=/mnt/nas/toprocess
   DEST_PATH=/mnt/nas/music
   PORT=5000
   ```
2. Build and start:
   ```sh
   docker compose up -d
   ```
3. Open `http://localhost:5000`

The DB is stored in `./data/music_organiser.db` and survives container restarts.

---

## Deployment to a Remote Server

A PowerShell deploy script (`deploy.ps1`) builds the image locally and transfers it via tar — no internet access needed on the server.

```powershell
.\deploy.ps1
```

This:
1. `docker build` → `docker save` to tar
2. Packages app files into a zip (`id3_organiser.py`, `templates/`, `enrichment/`, compose files)
3. `scp` both to remote `/tmp/`
4. SSH: `docker compose down` → `docker load` → `docker compose up -d`

The `data/` directory on the server is preserved across deploys.

**To wipe the DB and rescan from scratch:**
```powershell
ssh merlin@192.168.1.50 "cd /home/merlin/bin/id3 && docker compose down && rm -f data/music_organiser.db && docker compose up -d"
```

---

## CLI Options

```sh
python id3_organiser.py [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--db` | `music_organiser.db` | SQLite database path |
| `--port` | `5000` | Web server port |
| `--daily-limit` | `500` | Max MusicBrainz lookups per day |
| `--claude-limit` | `100` | Max Claude lookups per day |
| `--no-browser` | flag | Skip auto-opening browser |
| `--run-now` | flag | Trigger enrichment batch immediately on startup |
| `--poll-watcher` | flag | Use polling observer (required for NAS/network mounts) |
| `--watch-interval` | `60` | Poll interval in seconds (polling mode only) |

Paths (`/input`, `/destination`) are hardcoded for Docker use. When running locally, symlink or adjust the constants in `id3_organiser.py`.

---

## Requirements

- Python 3.12+
- See `requirements.txt`

```sh
pip install -r requirements.txt
python id3_organiser.py --db music_organiser.db
```

---

## License

MIT
