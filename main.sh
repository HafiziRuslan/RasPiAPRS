#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

dir_own=$(stat -c '%U' .)

log_msg() {
  local level=$1
  shift
  echo "$(date +'%FT%T') | $level | $*"
}

send_notification() {
  local message="⚠️ RasPiAPRS Alert: $1"
  local log_file="/var/log/raspiaprs/error.log"

  if [ -f "$log_file" ]; then
    local log_tail=$(tail -n 10 "$log_file")
    if [ -n "$log_tail" ]; then
      message="$message"$'\n\n'"Last 10 error log lines:"$'\n'"$log_tail"
    fi
  fi

  if [ -f .env ]; then
    local token=$(grep "^TELEGRAM_TOKEN=" .env | cut -d '=' -f2- | cut -d '#' -f1 | sed 's/^"//;s/"$//;s/^'"'"'//;s/'"'"'$//' | tr -d '[:space:]')
    local chat_id=$(grep "^TELEGRAM_CHAT_ID=" .env | cut -d '=' -f2- | cut -d '#' -f1 | sed 's/^"//;s/"$//;s/^'"'"'//;s/'"'"'$//' | tr -d '[:space:]')

    if [ -n "$token" ] && [ -n "$chat_id" ]; then
      local encoded_message=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$message")
      wget -qO- --post-data "chat_id=$chat_id&text=$encoded_message" "https://api.telegram.org/bot$token/sendMessage" >/dev/null 2>&1
    fi
  fi
}

check_internet() {
  local output
  if output=$(timeout 5 ping -q -c 3 -W 1 8.8.8.8 2>&1); then
    return 0
  else
    log_msg WARN "Internet check failed. Ping statistics: $output"
    return 1
  fi
}

check_disk_space() {
  local required_space_kb=102400 # 100MB
  local available_space_kb
  available_space_kb=$(df -kP . | awk 'NR==2 {print $4}')

  if [ "$available_space_kb" -lt "$required_space_kb" ]; then
    log_msg WARN "⚠️ Insufficient disk space for update. Required: ${required_space_kb}KB, Available: ${available_space_kb}KB."
    return 1
  fi
  return 0
}

cleanup() {
  rm -rf /tmp/raspiaprs
  rm -rf /var/log/raspiaprs
}
cleanup

if [ ! -d "/tmp/raspiaprs" ]; then
  mkdir -p /tmp/raspiaprs
  chown -hR $dir_own:$dir_own /tmp/raspiaprs
fi

if [ ! -d "/var/log/raspiaprs" ]; then
  mkdir -p /var/log/raspiaprs
  chown -hR $dir_own:$dir_own /var/log/raspiaprs
fi

if check_internet; then
  INTERNET_AVAILABLE=true
else
  INTERNET_AVAILABLE=false
  log_msg WARN "⚠️ No internet connection detected. Skipping updates."
fi

if [ "$INTERNET_AVAILABLE" = true ] && check_disk_space; then
  log_msg INFO "Checking for updates..."
  fetch_success=false
  for i in {1..3}; do
    if sudo -u $dir_own git fetch -q; then
      fetch_success=true
      break
    fi
    log_msg WARN "Git fetch failed (attempt $i/3). Retrying in 5 seconds..."
    sleep 5
  done

  if [ "$fetch_success" = false ]; then
    log_msg WARN "⚠️ Failed to fetch updates after multiple attempts. Skipping update check."
  else
    LOCAL=$(sudo -u $dir_own git rev-parse HEAD)
    REMOTE=$(sudo -u $dir_own git rev-parse @{u})

    if [ "$LOCAL" != "$REMOTE" ]; then
      log_msg INFO "Updating RaspiAPRS repository"
      UPDATE_SUCCESS=false
      if sudo -u $dir_own timeout 60 git pull --autostash -q; then
        UPDATE_SUCCESS=true
      else
        log_msg WARN "Git pull failed. Attempting to resolve conflicts by resetting to remote..."
        if sudo -u $dir_own git reset --hard @{u}; then
          UPDATE_SUCCESS=true
          log_msg INFO "Reset to remote successful."
        fi
      fi

      if [ "$UPDATE_SUCCESS" = true ] && [ "$(sudo -u $dir_own git rev-parse HEAD)" = "$REMOTE" ]; then
        log_msg INFO "Verifying repository integrity..."
        if sudo -u $dir_own git fsck --full >/dev/null 2>&1; then
          log_msg INFO "Update applied and verified. Restarting script..."
          exec "$0" "$@"
        else
          log_msg ERROR "Repository integrity check failed! Skipping restart."
        fi
      else
        log_msg WARN "Update failed or HEAD does not match remote. Skipping restart."
      fi
    else
      log_msg INFO "Repository is up to date."
    fi
  fi
fi

ensure_apt_packages() {
  local missing_packages=()
  for pkg in "$@"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing_packages+=("$pkg")
    fi
  done

  if [ ${#missing_packages[@]} -eq 0 ]; then
    log_msg INFO "✅ Packages are installed: $*."
  else
    log_msg WARN "❌ Missing packages: ${missing_packages[*]}. -> Installing missing packages"
    if [ "$INTERNET_AVAILABLE" = true ]; then
      apt-get update -q && apt-get install -y -q "$@"
    else
      log_msg ERROR "Cannot install missing packages without internet connection."
    fi
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_apt_packages gcc git python3-dev wget

if command_exists uv; then
  log_msg INFO "✅ uv is installed."
else
  if [ "$INTERNET_AVAILABLE" = true ]; then
    log_msg WARN "❌ uv is NOT installed. -> Installing uv"
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    log_msg ERROR "❌ uv is NOT installed and cannot be installed without internet."
    exit 1
  fi
fi

if [ ! -d ".venv" ]; then
  log_msg INFO "RasPiAPRS environment not found, creating one."
  uv venv
  log_msg INFO "Activating RasPiAPRS environment"
  source .venv/bin/activate
  if [ "$INTERNET_AVAILABLE" = true ]; then
    log_msg INFO "Installing RasPiAPRS dependencies"
    uv sync -q
  else
    log_msg WARN "Internet unavailable. Skipping dependency installation."
  fi
else
  log_msg INFO "RasPiAPRS environment already exists. -> Activating RasPiAPRS environment"
  source .venv/bin/activate
  if [ "$INTERNET_AVAILABLE" = true ]; then
    log_msg INFO "Updating RasPiAPRS dependencies"
    uv sync -q
  fi
fi

log_msg INFO "Running RasPiAPRS"
RESTART_DELAY=5
MAX_DELAY=300
MAX_RETRIES=10
RETRY_COUNT=0

while true; do
  START_TIME=$(date +%s)
  set +e
  uv run -s ./src/main.py
  exit_code=$?
  set -e
  END_TIME=$(date +%s)

  if [ $((END_TIME - START_TIME)) -gt 60 ]; then
    RESTART_DELAY=5
    RETRY_COUNT=0
  fi

  # Define exit codes that trigger a restart
  # 1: General error (e.g. Exception)
  # 137: SIGKILL (e.g. OOM)
  RESTART_CODES="1 137"
  should_restart=false
  for code in $RESTART_CODES; do
    if [ "$exit_code" -eq "$code" ]; then
      should_restart=true
      break
    fi
  done

  if [ "$should_restart" = true ]; then
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ "$RETRY_COUNT" -gt "$MAX_RETRIES" ]; then
      log_msg ERROR "Maximum retries ($MAX_RETRIES) reached. Exiting."
      send_notification "Maximum retries ($MAX_RETRIES) reached. Service stopping."
      exit 1
    fi

    log_msg ERROR "RasPiAPRS exited with code $exit_code. Retry $RETRY_COUNT/$MAX_RETRIES. Re-run in ${RESTART_DELAY} seconds."
    sleep $RESTART_DELAY

    RESTART_DELAY=$((RESTART_DELAY * 2))
    if [ "$RESTART_DELAY" -gt "$MAX_DELAY" ]; then
      RESTART_DELAY=$MAX_DELAY
    fi
    elif [ "$exit_code" -eq 0 ]; then
    log_msg INFO "RasPiAPRS exited normally. Stopping."
    break
  else
    log_msg ERROR "RasPiAPRS exited with unrecoverable code $exit_code. Stopping."
    send_notification "Script exited with unrecoverable code $exit_code. Service stopping."
    exit "$exit_code"
  fi
done
