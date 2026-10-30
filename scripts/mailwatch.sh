#!/usr/bin/env bash
# Background mail watch for the relay (pilot approved 2026-09-26,
# thread 57). A session runs this as a background task after its startup
# bind + /pending. It costs no tokens while waiting; when it exits, Claude
# Code re-invokes the session, which handles its mail and re-arms.
#
# Usage: mailwatch.sh <role> <after_id> [interval_seconds]
#   after_id: the highest message_id this session has already handled
#             (read, acked, or deliberately left unread).
#
# On its first successful check it notes the unread mail at or below
# after_id that has already been DELIVERED to this role (the watch or a
# nudge woke the session for it): that's mail the session deliberately
# left unread, and it never wakes the session again (no wake loop). ANY
# other unread message wakes it (exits 0): anything above after_id, a
# lower id that only appears later (ids are assigned when a post starts
# but become visible when it commits, so they can arrive out of order),
# and anything never delivered, whatever its id. So a wrong after_id (e.g.
# the id of your own post) costs at most one extra wake; it can't silence
# mail you haven't been told about (security-claude, pilot msg 746).
# Exits 2 with a message on bad input, an unknown role or a 4xx from the
# relay. If the relay is unreachable or erroring (5xx) it keeps waiting, but
# only for DOWN_LIMIT seconds (MAILWATCH_DOWN_LIMIT, default 300): then it
# exits 3, so the session is woken and told, instead of mail silently
# stopping. A session that gets exit 3 checks the relay, then re-arms.
# After its first check it waits for mail with POST /roles/<role>/wait: the
# relay holds that request until there is mail the watch hasn't seen, or for
# MAILWATCH_WAIT seconds (default 25), so mail wakes the session in about a
# second. Each wait is also the watch's heartbeat. Against a relay without
# /wait (404), or with MAILWATCH_WAIT=0, it polls instead: every interval it
# posts a heartbeat (POST /roles/<role>/heartbeat) and checks
# /pending?peek=true. Either way the dashboard shows the watch as running, or
# flags it once it stops; the heartbeat is what marks the role seen.
# When it wakes the session it marks those messages "delivered" for the
# role (WhatsApp-style: it reached the session). It never marks anything
# read; only the session does that, once it has actually read it.
# Prints message ids only (not bodies): the session pulls content itself.
set -u
usage="usage: mailwatch.sh <role> <after_id> [interval_seconds]"
ROLE=${1:?$usage}
AFTER=${2:?$usage}
INTERVAL=${3:-20}
# RELAY_URL env var, then a per-repo .relay-url file (same precedence as
# scripts/relay's resolve_role) -- a non-interactive shell doesn't source
# ~/.bashrc, so a checked-in file is what actually works everywhere.
URL=${RELAY_URL:-}
[ -z "$URL" ] && [ -f .relay-url ] && URL=$(tr -d '[:space:]' < .relay-url)
URL=${URL:-http://127.0.0.1:8089}
die() { echo "mailwatch: $*" >&2; exit 2; }

[[ $AFTER =~ ^[0-9]+$ ]] || die "after_id must be a number, got '$AFTER'"
[[ $INTERVAL =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 1 ] || die "interval must be a positive number, got '$INTERVAL'"
DOWN_LIMIT=${MAILWATCH_DOWN_LIMIT:-300}
[[ $DOWN_LIMIT =~ ^[0-9]+$ ]] || die "MAILWATCH_DOWN_LIMIT must be a number of seconds, got '$DOWN_LIMIT'"
WAIT=${MAILWATCH_WAIT:-25}
[[ $WAIT =~ ^[0-9]+$ ]] && [ "$WAIT" -le 25 ] || die "MAILWATCH_WAIT must be 0..25 seconds, got '$WAIT'"
use_wait=$([ "$WAIT" -gt 0 ] && echo 1)

down_since=   # when the relay stopped answering (empty while it answers)
# The relay didn't answer usefully (no connection, timeout, 5xx): wait one
# interval, or give up with exit 3 once it has been down for DOWN_LIMIT.
relay_down() {
  local now; now=$(date +%s)
  [ -z "$down_since" ] && down_since=$now
  if [ $((now - down_since)) -ge "$DOWN_LIMIT" ]; then
    echo "mailwatch: relay at $URL unreachable or erroring for $((now - down_since))s (last: HTTP $1)." >&2
    echo "Check the relay (relay pending; this watch used $URL), then re-arm: $0 $ROLE $AFTER" >&2
    exit 3
  fi
  sleep "$INTERVAL"
}

checked_role=
known=   # JSON array of delivered ids <= after_id left unread, set on first check
while :; do
  if [ -z "$checked_role" ]; then
    rc=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$URL/roles/$ROLE") || rc=000
    case $rc in
      200) checked_role=1 ;;
      404) die "role '$ROLE' has never been bound on the relay (typo?)" ;;
      *) relay_down "$rc"; continue ;;   # relay down: validate once it's back
    esac
  fi
  if [ -n "$known" ] && [ -n "$use_wait" ]; then
    # Long-poll: the relay answers when there is mail outside $known, or
    # after $WAIT seconds with none. It records the heartbeat itself.
    resp=$(curl -s -m $((WAIT + 15)) -w '\n%{http_code}' -X POST -H 'X-Relay-Client: mailwatch.sh' \
      -H 'Content-Type: application/json' -d "{\"known\": $known}" \
      "$URL/roles/$ROLE/wait?timeout=$WAIT") || resp=$'\n000'
  else
    # The first check, or polling (an older relay, or MAILWATCH_WAIT=0).
    curl -s -o /dev/null -m 5 -X POST -H 'X-Relay-Client: mailwatch.sh' "$URL/roles/$ROLE/heartbeat?interval=$INTERVAL" || true
    resp=$(curl -s -m 5 -w '\n%{http_code}' "$URL/roles/$ROLE/pending?peek=true") || resp=$'\n000'
  fi
  code=${resp##*$'\n'}
  body=${resp%$'\n'*}
  case $code in
    200) ;;
    404) if [ "$body" = '{"detail":"Not Found"}' ]; then use_wait=; continue; fi   # older relay: no /wait
         die "relay returned HTTP $code: $body" ;;
    4??) die "relay returned HTTP $code: $body" ;;
    *) relay_down "$code"; continue ;;
  esac
  down_since=
  if [ -z "$known" ]; then
    known=$(printf '%s' "$body" | jq -c --argjson after "$AFTER" \
      '[.pending[] | select(.message_id <= $after and .delivered_at != null) | .message_id]') \
      || die "unexpected response from relay: $body"
  fi
  new=$(printf '%s' "$body" | jq -r --argjson known "$known" '
      .pending[] | select(.message_id as $id | $known | any(. == $id) | not)
      | "\(.message_id) \(.thread_id) \(.sender_role)"') || die "unexpected response from relay: $body"
  if [ -n "$new" ]; then
    printf '%s\n' "$new" | while read -r id _; do
      curl -s -o /dev/null -m 5 -X POST -H 'X-Relay-Client: mailwatch.sh' "$URL/messages/$id/delivered?role=$ROLE" || true
    done
    echo "relay mail for $ROLE: $(printf '%s\n' "$new" | wc -l) new"
    printf '%s\n' "$new" | while read -r id thread from; do echo "  msg $id in thread $thread from $from"; done
    next=$(printf '%s\n' "$new" | awk -v a="$AFTER" '$1 > a { a = $1 } END { print a }')
    echo "Next: GET /roles/$ROLE/pending, read or ack each message, then re-arm:"
    echo "  $0 $ROLE $next"
    exit 0
  fi
  # In wait mode there is nothing to sleep for: a wait already waited, and
  # the first check goes straight on to waiting. Polling sleeps.
  [ -n "$use_wait" ] || sleep "$INTERVAL"
done
