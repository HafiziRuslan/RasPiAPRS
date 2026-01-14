#!/bin/bash
set -e
date=$(date +'%FT%T')
dir_own=$(stat -c '%U' .)

if [ ! -d "/tmp/raspiaprs" ]; then
  mkdir -p /tmp/raspiaprs
  chown -hR $dir_own:$dir_own /tmp/raspiaprs
fi

if [ ! -d "/var/log/raspiaprs" ]; then
  mkdir -p /var/log/raspiaprs
  chown -hR $dir_own:$dir_own /var/log/raspiaprs
fi

echo "$date | Updating RaspiAPRS repository"
sudo -u $dir_own git pull --autostash -q

ensure_apt_packages() {
  local missing_packages=()
  for pkg in "$@"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing_packages+=("$pkg")
    fi
  done

  if [ ${#missing_packages[@]} -eq 0 ]; then
    echo "$date | ✅ Packages are installed: $*."
  else
    echo -n "$date | ❌ Missing packages: ${missing_packages[*]}."
    echo " -> Installing missing packages"
    apt-get update -q && apt-get install -y -q "$@"
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_apt_packages gcc git python3-dev

if command_exists uv; then
  echo "$date | ✅ uv is installed."
else
  echo -n "$date | ❌ uv is NOT installed."
  echo " -> Installing uv"
  wget -qO- https://astral.sh/uv/install.sh | sh
fi

if [ ! -d ".venv" ]; then
  echo "$date | RasPiAPRS environment not found, creating one."
  uv venv
  echo "$date | Activating RasPiAPRS environment"
  source .venv/bin/activate
  echo "$date | Installing RasPiAPRS dependencies"
  uv sync
else
  echo -n "$date | RasPiAPRS environment already exists."
  echo " -> Activating RasPiAPRS environment"
  source .venv/bin/activate
  echo "$date | Updating RasPiAPRS dependencies"
  uv sync -q
fi

echo "$date | Running RasPiAPRS"
while true; do
  uv run -s ./main.py
  echo "$date | RasPiAPRS exited. Re-run in 30 seconds."
  sleep 30
done
