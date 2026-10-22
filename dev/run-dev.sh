#!/bin/bash
# Local preview: a fake JDownloader (dev/mock_jd.py) on 3129 and Nav Downloader on
# 127.0.0.1:PORT (default 30599). Each port gets a throwaway data folder,
# dev/.data-PORT, that starts with no password: the setup code is printed below.
# A sample "Save to" list stands in for real folders.
#
#   dev/run-dev.sh [PORT] [--no-jd]     --no-jd: no fake JDownloader, to see the Connect help
cd "$(dirname "$0")/.." || exit 1
port=30599
jd_port=3129
for arg in "$@"; do
  case "$arg" in
    --no-jd) jd_port=3199 ;;  # nothing listens there
    *) port=$arg ;;
  esac
done
data=dev/.data-$port
mkdir -p "$data"
cp dev/folders.sample.json "$data/folders.json"
pids=()
if [ "$jd_port" = 3129 ]; then
  python3 -I dev/mock_jd.py 3129 &
  pids+=($!)
fi
JD_API=http://127.0.0.1:$jd_port JDWEB_BIND=127.0.0.1 JDWEB_PORT=$port JDWEB_DATA=$data JDWEB_IMAGE=nav-downloader \
  python3 -I -u server.py &
server=$!
pids+=($server)
trap 'kill "${pids[@]}" 2>/dev/null' EXIT TERM INT
wait $server
