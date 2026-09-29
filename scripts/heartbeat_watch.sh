#!/usr/bin/env bash
# coordinator-claude's own monitoring tool -- NOT part of the shared protocol and
# not something other sessions run. Other sessions self-serve their own
# watch; this is coordinator-claude watching THEM, per a 2026-09-28
# exception (memory: feedback_sendmessage_nudge_exception.md) letting
# coordinator-claude SendMessage a peer directly when it's online (ListAgents)
# but its mail watch has stopped heartbeating.
#
# Polls the relay's dashboard state every INTERVAL_S seconds (no token
# cost while it waits, same idea as mailwatch.sh) and exits the moment it
# finds a role that is:
#   - status "live" (still bound, not superseded), and
#   - heartbeat_at older than STALE_S seconds (default 600 = 10 min)
# that it hasn't already reported for this exact heartbeat_at value.
#
# It does NOT check ListAgents itself (that's a Claude Code tool, not
# reachable from a shell script) -- coordinator-claude must still confirm the
# role's session is actually online before sending a nudge. A role can be
# "live" in the relay's sense with its session long closed; the relay
# alone can't tell you that.
#
# Usage: heartbeat_watch.sh <state-file> [interval_s=60] [stale_s=600]
# Exit 0: found a newly-stale role, printed as "role<TAB>session_name<TAB>heartbeat_at<TAB>age_s".
# Exit 2: usage error or the relay rejected a request outright (not "down").
set -u
API=${RELAY_URL:-http://127.0.0.1:8089}

if [ $# -lt 1 ]; then
  echo "usage: $0 <state-file> [interval_s=60] [stale_s=600]" >&2
  exit 2
fi
STATE=$1
INTERVAL=${2:-60}
STALE_S=${3:-600}

# STALE_S is interpolated straight into a python -c script below -- must
# be digits only, or it's arbitrary code injection via a crafted argument.
[[ $INTERVAL =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 1 ] || { echo "interval_s must be a positive integer, got '$INTERVAL'" >&2; exit 2; }
[[ $STALE_S =~ ^[0-9]+$ ]] || { echo "stale_s must be a non-negative integer, got '$STALE_S'" >&2; exit 2; }

touch "$STATE" || { echo "can't write state file $STATE" >&2; exit 2; }

while true; do
  resp=$(curl -s -w '\n%{http_code}' "$API/api/dashboard/state" 2>/dev/null)
  code=$(echo "$resp" | tail -1)
  body=$(echo "$resp" | sed '$d')
  if [ "$code" != "200" ]; then
    sleep "$INTERVAL"
    continue
  fi

  hit=$(echo "$body" | python3 -c "
import json, sys, datetime
d = json.load(sys.stdin)
now = datetime.datetime.now(datetime.timezone.utc)
stale_s = $STALE_S
for r in d.get('roles', []):
    if r.get('status') != 'live':
        continue
    hb = r.get('heartbeat_at')
    if not hb:
        continue
    hb_dt = datetime.datetime.fromisoformat(hb.replace('Z', '+00:00'))
    age = (now - hb_dt).total_seconds()
    if age >= stale_s:
        print(f\"{r['role']}\t{r.get('session_name','')}\t{hb}\t{int(age)}\")
" 2>/dev/null)

  while IFS=$'\t' read -r role session hb age; do
    [ -z "$role" ] && continue
    key="$role|$hb"
    if ! grep -qxF "$key" "$STATE"; then
      echo "$key" >> "$STATE"
      printf '%s\t%s\t%s\t%s\n' "$role" "$session" "$hb" "$age"
      exit 0
    fi
  done <<< "$hit"

  sleep "$INTERVAL"
done
