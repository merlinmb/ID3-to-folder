#!/bin/bash
# Deploy script for ID3-to-folder project
# Usage: ./deploy.sh

set -e

REMOTE_USER="merlin"
REMOTE_HOST="192.168.1.50"
REMOTE_DIR="/home/merlin/bin/id3"
VENV_DIR="$REMOTE_DIR/venv"

# Rsync project files, exclude venv, __pycache__, and other unnecessary files
rsync -avz --exclude 'venv/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.pyo' --exclude '*.swp' --exclude '.git/' --exclude '.DS_Store' ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

# Create venv if it does not exist, then install requirements
ssh "$REMOTE_USER@$REMOTE_HOST" "\
  cd $REMOTE_DIR && \
  if [ ! -d venv ]; then \
    python3 -m venv venv; \
  fi && \
  source venv/bin/activate && \
  pip install --upgrade pip && \
  pip install -r requirements.txt
"

echo "Deployment complete."
