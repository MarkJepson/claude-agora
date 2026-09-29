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
# relay. If the relay is unreachable or erroring (5xx), it keeps waiting.
# Every loop it also posts a heartbeat (POST /roles/<role>/heartbeat), so
# the dashboard shows the watch as running, or flags it once it stops.
# Mail is checked with peek=true: the heartbeat is what marks the role seen.
# When it wakes the session it marks those messages "delivered" for the
# role (WhatsApp-style: it reached the session). It never marks anything
# read; only the session does that, once it has actually read it.
# Prints message ids only (not bodies): the session pulls content itself.
set -u
usage="usage: mailwatch.sh <role> <after_id> [interval_seconds]"
ROLE=${1:?$usage}
AFTER=${2:?$usage}
INTERVAL=${3:-20}
URL=${RELAY_URL:-http://127.0.0.1:8089}
die() { echo "mailwatch: $*" >&2; exit 2; }

[[ $AFTER =~ ^[0-9]+$ ]] || die "after_id must be a number, got '$AFTER'"
[[ $INTERVAL =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 1 ] || die "interval must be a positive number, got '$INTERVAL'"

checked_role=
known=   # JSON array of delivered ids <= after_id left unread, set on first check
while :; do
  if [ -z "$checked_role" ]; then
    rc=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$URL/roles/$ROLE") || rc=000
    case $rc in
      200) checked_role=1 ;;
      404) die "role '$ROLE' has never been bound on the relay (typo?)" ;;
      *) sleep "$INTERVAL"; continue ;;   # relay down: validate once it's back
    esac
  fi
  curl -s -o /dev/null -m 5 -X POST -H 'X-Relay-Client: mailwatch.sh' "$URL/roles/$ROLE/heartbeat?interval=$INTERVAL" || true
  resp=$(curl -s -m 5 -w '\n%{http_code}' "$URL/roles/$ROLE/pending?peek=true") || resp=$'\n000'
  code=${resp##*$'\n'}
  body=${resp%$'\n'*}
  case $code in
    200) ;;
    4??) die "relay returned HTTP $code: $body" ;;
    *) sleep "$INTERVAL"; continue ;;
  esac
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
  sleep "$INTERVAL"
done
