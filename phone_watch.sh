#!/data/data/com.termux/files/usr/bin/bash
# Keeps checking BookMyShow every 5 minutes on an Android phone (Termux).
# Usage: bash phone_watch.sh        (needs a .ntfy_topic file next to it)
cd "$(dirname "$0")"

export BMS_URL="https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00439706"
export BMS_SCREEN_RULES="prasads=pcx|hdr by barco; allu=dolby cinema|dolby vision"
export BMS_ALL_FORMATS="true"

if [ ! -s .ntfy_topic ]; then
  echo "Missing .ntfy_topic file. Create it with:  echo YOUR-TOPIC > .ntfy_topic"
  exit 1
fi
export NTFY_TOPIC="$(cat .ntfy_topic)"

# Stop Android from pausing Termux while the screen is off
command -v termux-wake-lock >/dev/null && termux-wake-lock

while true; do
  echo "===== $(date '+%d %b %H:%M:%S')"
  python main.py 2>&1 | grep -vE "^\s+(Prasads|ALLU|Current status)"
  sleep 300
done
