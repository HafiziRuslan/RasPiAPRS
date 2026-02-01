#!/bin/bash

echo "RasPiAPRS Troubleshooting Script"
echo "================================"

# Check for root privileges
if [ "$EUID" -ne 0 ]; then
  echo "[WARN] Not running as root. Some checks might fail or 'main.sh' might not run correctly."
  echo "       RasPiAPRS usually requires sudo to access /var/log."
else
  echo "[PASS] Running as root."
fi

get_env_var() {
  local var_name="$1"
  if [ -f .env ]; then
    local val
    val=$(grep "^${var_name}=" .env | head -n 1 | cut -d '=' -f2-)
    # Trim leading whitespace
    val="${val#"${val%%[![:space:]]*}"}"

    if [[ "$val" == \"* ]]; then
      val="${val#\"}"
      val="${val%%\"*}"
    elif [[ "$val" == \'* ]]; then
      val="${val#\'}"
      val="${val%%\'*}"
    else
      val="${val%%#*}"
      val="${val%"${val##*[![:space:]]}"}"
    fi
    echo "$val"
  fi
}

# Check for .env file
if [ -f ".env" ]; then
  echo "[PASS] .env configuration file found."

  # Check APRS Credentials
  CALLSIGN=$(get_env_var "APRS_CALLSIGN" | tr -d '[:space:]')
  PASSCODE=$(get_env_var "APRS_PASSCODE" | tr -d '[:space:]')

  if [ -n "$CALLSIGN" ]; then
    if [[ "$CALLSIGN" =~ ^[A-Z0-9]+(-[A-Z0-9]+)?$ ]]; then
      echo "[PASS] APRS_CALLSIGN found: $CALLSIGN"
    else
      echo "[WARN] APRS_CALLSIGN '$CALLSIGN' format looks invalid."
    fi
  else
    echo "[FAIL] APRS_CALLSIGN is missing or empty in .env."
  fi

  if [ -n "$PASSCODE" ] && [[ "$PASSCODE" =~ ^-?[0-9]+$ ]]; then
    echo "[PASS] APRS_PASSCODE found."
  else
    echo "[FAIL] APRS_PASSCODE is missing or invalid (must be numeric) in .env."
  fi

  # Check APRS SSID
  SSID=$(get_env_var "APRS_SSID" | tr -d '[:space:]')
  if [ -n "$SSID" ]; then
    if [[ "$SSID" =~ ^[0-9]+$ ]] && [ "$SSID" -ge 0 ] && [ "$SSID" -le 15 ]; then
      echo "[PASS] APRS_SSID '$SSID' is valid (0-15)."
    else
      echo "[FAIL] APRS_SSID '$SSID' is invalid (must be 0-15)."
    fi
  else
    echo "[WARN] APRS_SSID is missing in .env."
  fi

  # Check Serial Port
  SERIAL_PORT=$(get_env_var "SERIAL_PORT" | tr -d '[:space:]')
  if [ -n "$SERIAL_PORT" ]; then
    if [ -e "$SERIAL_PORT" ]; then
      echo "[PASS] Serial port '$SERIAL_PORT' found."
    else
      echo "[FAIL] Serial port '$SERIAL_PORT' configured but NOT found."
    fi
  fi

  # Check GPSD
  GPSD_ENABLE=$(get_env_var "GPSD_ENABLE" | tr -d '[:space:]')
  if [ "$GPSD_ENABLE" = "true" ]; then
    GPSD_HOST=$(get_env_var "GPSD_HOST" | tr -d '[:space:]')
    GPSD_PORT=$(get_env_var "GPSD_PORT" | tr -d '[:space:]')
    [ -z "$GPSD_HOST" ] && GPSD_HOST="localhost"
    [ -z "$GPSD_PORT" ] && GPSD_PORT="2947"
    if timeout 1 bash -c "</dev/tcp/$GPSD_HOST/$GPSD_PORT" 2>/dev/null; then
      echo "[PASS] GPSD reachable at $GPSD_HOST:$GPSD_PORT."
    else
      echo "[FAIL] GPSD enabled but NOT reachable at $GPSD_HOST:$GPSD_PORT."
    fi
  fi

  # Check SmartBeaconing
  SB_ENABLE=$(get_env_var "SMARTBEACONING_ENABLE" | tr -d '[:space:]')
  if [ "$SB_ENABLE" = "true" ]; then
    SB_VARS=("SMARTBEACONING_FASTSPEED" "SMARTBEACONING_FASTRATE" "SMARTBEACONING_SLOWSPEED" "SMARTBEACONING_SLOWRATE" "SMARTBEACONING_MINTURNTIME" "SMARTBEACONING_MINTURNANGLE" "SMARTBEACONING_TURNSLOPE")
    SB_FAIL=0
    for var in "${SB_VARS[@]}"; do
      val=$(get_env_var "$var" | tr -d '[:space:]')
      if ! [[ "$val" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
         echo "[FAIL] $var is invalid or missing (must be numeric). Got: '$val'"
         SB_FAIL=1
      fi
    done
    [ $SB_FAIL -eq 0 ] && echo "[PASS] SmartBeaconing settings look valid."
  fi

  # Check APRS Symbol
  APRS_SYMBOL=$(get_env_var "APRS_SYMBOL")
  APRS_SYMBOL_TABLE=$(get_env_var "APRS_SYMBOL_TABLE")

  if [ -n "$APRS_SYMBOL" ] && [ -n "$APRS_SYMBOL_TABLE" ]; then
    if [ ${#APRS_SYMBOL} -eq 1 ] && [ ${#APRS_SYMBOL_TABLE} -eq 1 ]; then
      if [[ "$APRS_SYMBOL_TABLE" =~ ^[0-9A-Z/\\\&]$ ]]; then
        echo "[PASS] APRS_SYMBOL_TABLE '$APRS_SYMBOL_TABLE' is valid format."
      else
        echo "[WARN] APRS_SYMBOL_TABLE '$APRS_SYMBOL_TABLE' might be invalid (expected /, \, &, 0-9, or A-Z)."
      fi

      SYMBOL_FILE="misc/symbolsX.txt"
      if [ -f "$SYMBOL_FILE" ]; then
        [ "$APRS_SYMBOL_TABLE" = "/" ] && LOOKUP_STR="/${APRS_SYMBOL}" || LOOKUP_STR="\\${APRS_SYMBOL}"
        if grep -Fq "$LOOKUP_STR" "$SYMBOL_FILE"; then
           echo "[PASS] APRS_SYMBOL '$APRS_SYMBOL' found in symbol table."
        else
           echo "[WARN] APRS_SYMBOL '$APRS_SYMBOL' not found in $SYMBOL_FILE (Table: $APRS_SYMBOL_TABLE). It might be custom or new."
        fi
      else
        [[ "$APRS_SYMBOL" =~ ^[!-~]$ ]] && echo "[PASS] APRS_SYMBOL '$APRS_SYMBOL' is valid format." || echo "[FAIL] APRS_SYMBOL '$APRS_SYMBOL' is invalid."
      fi
    else
      echo "[FAIL] APRS_SYMBOL or APRS_SYMBOL_TABLE must be exactly one character."
    fi
  else
    echo "[WARN] APRS_SYMBOL or APRS_SYMBOL_TABLE not set in .env."
  fi

  # Check APRS-IS Reachability
  APRSIS_SERVER=$(get_env_var "APRSIS_SERVER" | tr -d '[:space:]')
  APRSIS_PORT=$(get_env_var "APRSIS_PORT" | tr -d '[:space:]')

  if [ -n "$APRSIS_SERVER" ] && [ -n "$APRSIS_PORT" ]; then
    if timeout 2 bash -c "</dev/tcp/$APRSIS_SERVER/$APRSIS_PORT" 2>/dev/null; then
      echo "[PASS] APRS-IS Server $APRSIS_SERVER:$APRSIS_PORT is reachable."
    else
      echo "[FAIL] APRS-IS Server $APRSIS_SERVER:$APRSIS_PORT is NOT reachable."
    fi
  else
    echo "[WARN] APRSIS_SERVER or APRSIS_PORT not set in .env."
  fi

  # Check APRS Coordinates
  LATITUDE=$(get_env_var "APRS_LATITUDE" | tr -d '[:space:]')
  LONGITUDE=$(get_env_var "APRS_LONGITUDE" | tr -d '[:space:]')

  if [ -n "$LATITUDE" ] && [ -n "$LONGITUDE" ]; then
    if [[ "$LATITUDE" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && [[ "$LONGITUDE" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
      if awk -v lat="$LATITUDE" -v lon="$LONGITUDE" 'BEGIN {exit !(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)}'; then
        echo "[PASS] APRS Coordinates look valid: $LATITUDE, $LONGITUDE"
      else
        echo "[FAIL] APRS Coordinates out of range (Lat: -90 to 90, Lon: -180 to 180). Got: $LATITUDE, $LONGITUDE"
      fi
    else
      echo "[FAIL] APRS Coordinates format invalid (must be numeric)."
    fi
  else
    echo "[WARN] APRS_LATITUDE or APRS_LONGITUDE is missing in .env."
  fi

  # Check APRS Altitude
  ALTITUDE=$(get_env_var "APRS_ALTITUDE" | tr -d '[:space:]')
  if [ -n "$ALTITUDE" ]; then
    if [[ "$ALTITUDE" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
      echo "[PASS] APRS_ALTITUDE '$ALTITUDE' is valid."
    else
      echo "[FAIL] APRS_ALTITUDE '$ALTITUDE' is invalid (must be numeric)."
    fi
  else
    echo "[WARN] APRS_ALTITUDE is missing in .env."
  fi

  # Check Telegram
  TELEGRAM_ENABLE=$(get_env_var "TELEGRAM_ENABLE" | tr -d '[:space:]')
  if [ "$TELEGRAM_ENABLE" = "true" ]; then
    TELEGRAM_TOKEN=$(get_env_var "TELEGRAM_TOKEN" | tr -d '[:space:]')
    if [[ "$TELEGRAM_TOKEN" =~ ^[0-9]+:[a-zA-Z0-9_-]+$ ]]; then
      echo "[PASS] TELEGRAM_TOKEN format looks valid."
    else
      echo "[FAIL] TELEGRAM_TOKEN format is invalid (expected 123456:ABC-DEF...)."
    fi

    TELEGRAM_CHAT_ID=$(get_env_var "TELEGRAM_CHAT_ID" | tr -d '[:space:]')
    if [[ "$TELEGRAM_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
      echo "[PASS] TELEGRAM_CHAT_ID format looks valid."
    else
      echo "[FAIL] TELEGRAM_CHAT_ID format is invalid (must be numeric)."
    fi
  fi
else
  echo "[FAIL] .env configuration file NOT found."
  echo "       Please copy .env.sample to .env and configure it."
  echo "       Command: cp .env.sample .env"
fi

# Check for main.sh executable
if [ -x "main.sh" ]; then
  echo "[PASS] main.sh is executable."
else
  echo "[WARN] main.sh is not executable."
  echo "       Fixing permissions..."
  chmod +x main.sh
  if [ -x "main.sh" ]; then
    echo "[PASS] main.sh is now executable."
  else
    echo "[FAIL] Could not make main.sh executable."
  fi
fi

# Check dependencies
DEPENDENCIES=("gcc" "git" "curl" "uv")
MISSING_DEPS=0

for dep in "${DEPENDENCIES[@]}"; do
  if command -v $dep &> /dev/null; then
    echo "[PASS] Dependency '$dep' found."
  else
    echo "[FAIL] Dependency '$dep' NOT found."
    MISSING_DEPS=1
  fi
done

if [ $MISSING_DEPS -eq 1 ]; then
  echo "[WARN] Some dependencies are missing. The startup script usually installs them,"
  echo "       but you can try installing them manually."
fi

# Check log directory permissions
if [ -w "/var/log" ]; then
  echo "[PASS] /var/log is writable."
else
  echo "[FAIL] /var/log is NOT writable. Run with sudo."
fi

# Check tmp directory permissions
if [ -w "/tmp" ]; then
  echo "[PASS] /tmp is writable."
else
  echo "[FAIL] /tmp is NOT writable."
fi

echo "================================"
echo "Troubleshooting complete."