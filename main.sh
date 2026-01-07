#!/bin/bash
set -e
date=$(date +'%FT%T')
dir_own=$(stat -c '%U' .)

echo "$date | Updating files"
sudo -u $dir_own git pull --autostash -q

if [ ! -d "/tmp/raspiaprs" ]; then
  mkdir -p /tmp/raspiaprs
  chown -hR $dir_own /tmp/raspiaprs
fi

if [ ! -d "/log/raspiaprs" ]; then
  mkdir -p /log/raspiaprs
  chown -hR $dir_own /log/raspiaprs
fi

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

if command_exists uv; then
  echo "$date | ✅ uv is installed."
else
  echo -n "$date | ❌ uv is NOT installed."
  echo " -> Installing uv"
  wget -qO- https://astral.sh/uv/install.sh | sh
fi

if [ ! -d ".venv" ]; then
  echo "$date | Virtual environment not found, creating one."
  uv venv
  echo "$date | Activating virtual environment"
  source .venv/bin/activate
  echo "$date | Installing dependencies"
  uv sync
else
  echo -n "$date | Virtual environment already exists."
  echo " -> Activating virtual environment"
  source .venv/bin/activate
  echo "$date | Updating dependencies"
  uv sync -q
fi

echo "$date | Running main.py"
while true; do
  uv run -s ./main.py
  echo "$date | Script exited. Re-running in 30 seconds."
  sleep 30
done
