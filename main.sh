#!/bin/bash
set -e

# --- Constants and Globals ---
LOG_FILE="/var/log/RasPiAPRS.log"
RESTART_DELAY=5
MAX_DELAY=300
MAX_RETRIES=10
dir_own=""
INTERNET_AVAILABLE=false

# --- Logging and Utilities ---
log_msg() {
  local level=$1; shift
  local message="$*"
  local timestamp
  timestamp=$(date +'%Y-%m-%dT%H:%M:%S.%3N%:z')
  local level_padded
  level_padded=$(printf '%-8s' "$level")
  local thread_padded
  thread_padded=$(printf '%-12s' "$$")

  # caller gives: "<line> <func> <file>"
  local caller_info line_no func_name file_name
  caller_info=$(caller 0)
  read -r line_no func_name file_name <<<"$caller_info"
  local name_part="${file_name##*/}.${func_name}:${line_no}"

  printf '%s | %s | %s | %s | %s\n' \
    "$timestamp" "$level_padded" "$thread_padded" "$name_part" "$message"
}

get_env_var() {
  local var_name=$1
  if [ -f .env ]; then
    # strip comments and surrounding quotes
    sed -n "s/^${var_name}=//p" .env \
      | cut -d'#' -f1 \
      | sed "s/^['\"]\{0,1\}//;s/['\"]\{0,1\}$//"
  fi
}

# --- Setup and Pre-flight ---
setup_environment() {
  if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
  fi

  # save original stdout so that the pager/updater can still use it
  exec 3>&1
  exec >>"$LOG_FILE" 2>&1

  # make sure we are running from the repository root
  cd "$(dirname "$0")"
  dir_own=$(stat -c '%U' .)
}

cleanup() {
  rm -rf /var/tmp/RasPiAPRS
}

setup_directories() {
  cleanup
  local dirs=("/var/tmp/RasPiAPRS" "/var/log/RasPiAPRS" "/var/lib/RasPiAPRS")
  for dir in "${dirs[@]}"; do
    if [ ! -d "$dir" ]; then
      mkdir -p "$dir"
      chown -hR "$dir_own:$dir_own" "$dir"
    fi
  done
}

# --- System Checks ---
check_internet() {
  local hosts=(1.1.1.1 8.8.8.8 github.com pypi.org)
  for host in "${hosts[@]}"; do
    if timeout 5 ping -q -c 1 -W 1 "$host" >/dev/null 2>&1; then
      return 0
    fi
  done

  log_msg WARN "Internet check failed. Could not reach any of: ${hosts[*]}"
  return 1
}

check_disk_space() {
  local required_mb=100
  local available_mb
  available_mb=$(df -mP . | tail -n1 | awk '{print $4}')

  if ! [[ $available_mb =~ ^[0-9]+$ ]]; then
    log_msg WARN "⚠️ Could not determine available disk space."
    return 1
  fi

  if [ "$available_mb" -lt "$required_mb" ]; then
    log_msg WARN "⚠️ Insufficient disk space for update. " \
      "Required: ${required_mb}MB, Available: ${available_mb}MB."
    return 1
  fi
  return 0
}

# --- Dependency and Application Management ---
command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_apt_packages() {
  local missing=()
  for pkg in "$@"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done

  if [ ${#missing[@]} -eq 0 ]; then
    log_msg INFO "✅ Packages are installed: $*."
  else
    log_msg WARN "❌ Missing packages: ${missing[*]}. -> Installing"
    if [ "$INTERNET_AVAILABLE" = true ]; then
      apt-get update -q && apt-get install -y -q "${missing[@]}"
    else
      log_msg ERROR "Cannot install missing packages without internet connection."
    fi
  fi
}

ensure_uv_installed() {
  if command_exists uv; then
    log_msg INFO "✅ uv is installed."
  elif [ "$INTERNET_AVAILABLE" = true ]; then
    log_msg WARN "❌ uv is NOT installed. -> Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    log_msg ERROR "❌ uv is NOT installed and cannot be installed without internet."
    exit 1
  fi
}

sync_dependencies() {
  local action=$1
  if [ "$INTERNET_AVAILABLE" = true ]; then
    log_msg INFO "$action RasPiAPRS dependencies"
    sudo -u "$dir_own" uv tool run pyclean . -d -q
    sudo -u "$dir_own" uv sync -q
  elif [ "$action" = "Installing" ]; then
    log_msg WARN "Internet unavailable. Skipping dependency installation."
  fi
}

update_application() {
  if [ "$INTERNET_AVAILABLE" = false ] || ! check_disk_space; then
    log_msg INFO "Skipping application update check due to no internet or insufficient disk space."
    return
  fi
  log_msg INFO "Checking for updates..."
  fetch_success=false
  for i in {1..3}; do
    if sudo -u "$dir_own" git fetch -q; then
      fetch_success=true
      break
    fi
    log_msg WARN "Git fetch failed (attempt $i/3). Retrying in 5 seconds..."
    sleep 5
  done

  if [ "$fetch_success" = false ]; then
    log_msg WARN "⚠️ Failed to fetch updates after multiple attempts. Skipping update check."
  else
    LOCAL=$(sudo -u "$dir_own" git rev-parse HEAD)
    REMOTE=$(sudo -u "$dir_own" git rev-parse @{u})

    if [ "$LOCAL" != "$REMOTE" ]; then
      log_msg INFO "Updating RaspiAPRS"
      UPDATE_SUCCESS=false
      if sudo -u "$dir_own" timeout 60 git pull --autostash -q; then
        UPDATE_SUCCESS=true
      else
        log_msg WARN "Git pull failed. Attempting to resolve conflicts by resetting to remote..."
        if sudo -u "$dir_own" git reset --hard @{u}; then
          UPDATE_SUCCESS=true
          log_msg INFO "Reset to remote successful."
        fi
      fi

      if [ "$UPDATE_SUCCESS" = true ] && [ "$(sudo -u "$dir_own" git rev-parse HEAD)" = "$REMOTE" ]; then
        if ! sudo -u "$dir_own" git diff --quiet "$LOCAL" HEAD -- pyproject.toml; then
          log_msg INFO "Application updated. Forcing environement recreation."
          sudo -u "$dir_own" uv venv -c
        fi

        log_msg INFO "Verifying application integrity..."
        if sudo -u "$dir_own" git fsck --full >/dev/null 2>&1; then
          log_msg INFO "Update applied and verified. Restarting script..."
          exec "$0" "$@"
        else
          log_msg ERROR "Application integrity check failed! Skipping restart."
        fi
      else
        log_msg WARN "Update failed or HEAD does not match remote. Skipping restart."
      fi
    else
      log_msg INFO "Application is up to date."
    fi
  fi
}

setup_venv() {
  if [ -d ".venv" ]; then
    if ! sudo -u "$dir_own" ./.venv/bin/python3 -c 'import sys' >/dev/null 2>&1; then
      log_msg WARN "⚠️ Virtual environment appears corrupted. Removing it..."
      sudo -u "$dir_own" uv venv -c
    fi
  fi

  if [ ! -d ".venv" ]; then
    log_msg INFO "RasPiAPRS environment not found, creating one."
    sudo -u "$dir_own" uv venv
    log_msg INFO "Activating RasPiAPRS environment"
    sync_dependencies "Installing"
  else
    log_msg INFO "RasPiAPRS environment exists. -> Activating RasPiAPRS environment"
    sync_dependencies "Updating"
  fi
}

# --- Main Execution ---
run_app() {
  log_msg INFO "Running RasPiAPRS"
  local RETRY_COUNT=0

  while true; do
    local START_TIME
    START_TIME=$(date +%s)
    set +e
    sudo -u "$dir_own" uv run -s ./src/main.py
    local exit_code=$?
    set -e
    local END_TIME
    END_TIME=$(date +%s)

    if [ $((END_TIME - START_TIME)) -gt 60 ]; then
      RESTART_DELAY=5
      RETRY_COUNT=0
    fi

    local should_restart=false
    if [ "$exit_code" -ne 0 ] && [ "$exit_code" -ne 130 ] && [ "$exit_code" -ne 143 ]; then
      should_restart=true
    fi

    if [ "$should_restart" = true ]; then
      RETRY_COUNT=$((RETRY_COUNT + 1))
      if [ "$RETRY_COUNT" -gt "$MAX_RETRIES" ]; then
        log_msg ERROR "Maximum retries ($MAX_RETRIES) reached. Exiting."
        exit 1
      fi

      log_msg ERROR "RasPiAPRS exited with code $exit_code. Retry $RETRY_COUNT/$MAX_RETRIES. Re-run in ${RESTART_DELAY} seconds."
      sleep "$RESTART_DELAY"

      RESTART_DELAY=$((RESTART_DELAY * 2))
      if [ "$RESTART_DELAY" -gt "$MAX_DELAY" ]; then
        RESTART_DELAY=$MAX_DELAY
      fi
    elif [ "$exit_code" -eq 0 ]; then
      log_msg INFO "RasPiAPRS exited normally. Stopping."
      break
    else
      log_msg ERROR "RasPiAPRS exited with unrecoverable code $exit_code. Stopping."
      exit "$exit_code"
    fi
  done
}

main() {
  setup_environment
  setup_directories

  if check_internet; then
    INTERNET_AVAILABLE=true
  else
    log_msg WARN "⚠️ No internet connection detected. Skipping updates."
  fi

  ensure_apt_packages gcc git python3-dev curl vnstat
  ensure_uv_installed
  update_application
  setup_venv

  if [ ! -f .env ]; then
    log_msg ERROR "❌ .env file not found! Please copy .env.sample to .env and configure it."
    exit 1
  fi

  run_app
}

main "$@"
