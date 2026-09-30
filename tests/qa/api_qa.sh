#!/usr/bin/env bash
# End-to-end QA of the relay API against the live relay.
# Throwaway group = {coordinator-claude} only (a one-member group: how a session
# talks to the operator alone), so no other session is disturbed. 'operator'
# is never a member (the operator isn't a role); it appears only as the
# sender of posts, like the operator's dashboard posts. Leaves the group for
# ui_qa.mjs; delete it after.
set -u
A=${RELAY_URL:-http://127.0.0.1:8089}
echo "Running against $A -- this writes real data to that relay's database (no sandbox/mock)." >&2
echo "A throwaway group/thread is left behind on purpose for ui_qa.mjs; the summary line at the end prints how to delete it." >&2
DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$DIR/../.." && pwd)
W="$REPO_ROOT/scripts/mailwatch.sh"
ME=coordinator-claude
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1  -- $2"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected [$3] got [$2]"; }
code() { curl -s -o /tmp/qa_body -w '%{http_code}' -H 'X-Relay-Client: qa' "$@"; }
post() { curl -s -X POST "$A$1" -H 'Content-Type: application/json' -H 'X-Relay-Client: qa' -d "$2"; }
db()   { (cd "$REPO_ROOT" && docker compose exec -T db psql -U agora -d agora -At -c "$1"); }
mine() { curl -s "$A/roles/$ME/pending?peek=true" | jq -c "[.pending[] | select(.thread_id==$T) | .message_id]"; }

# Roles this suite exercises. Registering a role has no API (deliberately
# -- see BUILD.md), so QA seeds its own via direct insert, same as a human
# operator would for a real role.
for r in coordinator-claude architect-claude security-claude observability-claude; do
  db "INSERT INTO roles (role) VALUES ('$r') ON CONFLICT DO NOTHING" >/dev/null
done

echo "== health / routes"
eq "health"                    "$(curl -s $A/health)" '{"status":"ok"}'
eq "GET /dashboard 200"        "$(code $A/dashboard)" 200
eq "GET /dashboard/threads/1"  "$(code $A/dashboard/threads/1)" 200
eq "static js 200"             "$(code $A/dashboard/static/dashboard.js)" 200
eq "static whitelist 404"      "$(code $A/dashboard/static/main.py)" 404
eq "static traversal 404"      "$(code "$A/dashboard/static/..%2Fmain.py")" 404
eq "classic fallback 200"      "$(code $A/dashboard/classic)" 200
eq "CSP on dashboard page"     "$(curl -s -D - -o /dev/null $A/dashboard | grep -ci "^content-security-policy: default-src 'self'; script-src 'self'")" 1
eq "CSP on dashboard js"       "$(curl -s -D - -o /dev/null $A/dashboard/static/dashboard.js | grep -ci '^content-security-policy:')" 1

echo "== operator isn't a role"
eq "operator as group member 422"  "$(code -X POST $A/groups -H 'Content-Type: application/json' -d '{"members":["coordinator-claude","operator"]}')" 422
eq "bind operator 422"             "$(code -X POST $A/roles/operator/bind -H 'Content-Type: application/json' -d '{"session_name":"x"}')" 422
eq "heartbeat operator 404"        "$(code -X POST "$A/roles/operator/heartbeat?interval=20")" 404

echo "== bind + last_seen"
SESSION=$(curl -s $A/roles/$ME | jq -r .session_name)
eq "re-bind (idempotent)"      "$(post /roles/$ME/bind "{\"session_name\":\"$SESSION\"}")" '{"ok":true}'
eq "whois still live"          "$(curl -s $A/roles/$ME | jq -r .status)" live
eq "last action on bind"       "$(db "SELECT last_seen_action FROM roles WHERE role='$ME'")" "registered $SESSION"

echo "== groups: get-or-create"
R=$(post /groups '{"members":["security-claude","architect-claude","coordinator-claude"]}')
GABC=$(echo "$R" | jq -r .group_id)
[ "$GABC" -gt 0 ] 2>/dev/null && ok "create/get group {security,architect,coordinator}" || bad "create/get group {security,architect,coordinator}" "$R"
eq "duplicates collapse -> existing" "$(post /groups '{"members":["coordinator-claude","architect-claude","security-claude","coordinator-claude"]}' | jq -c '[.group_id,.existing]')" "[$GABC,true]"
eq "empty members 422"         "$(code -X POST $A/groups -H 'Content-Type: application/json' -d '{"members":[]}')" 422
PRE=$(db "SELECT g.group_id FROM groups g JOIN group_members gm USING (group_id) WHERE gm.left_at IS NULL AND g.status='active' GROUP BY g.group_id HAVING array_agg(gm.role) = ARRAY['$ME']" | head -1)
R=$(post /groups "{\"members\":[\"$ME\"]}")
G=$(echo "$R" | jq -r .group_id)
if [ -z "$PRE" ]; then eq "one-member group created (existing=false)" "$(echo "$R" | jq -r .existing)" false
else eq "one-member group reused (existing=true)" "$(echo "$R" | jq -c '[.group_id,.existing]')" "[$PRE,true]"; fi
eq "one-member group: same set again -> existing" "$(post /groups "{\"members\":[\"$ME\"]}" | jq -c '[.group_id,.existing]')" "[$G,true]"
eq "members"                   "$(curl -s $A/groups/$G/members | jq -c .members)" "[\"$ME\"]"
eq "add operator as member 422"    "$(code -X POST $A/groups/$G/members -H 'Content-Type: application/json' -d '{"role":"operator"}')" 422
T=$(post /groups/$G/threads '{"topic":"QA run (coordinator-claude only, auto-deleted)"}' | jq -r .thread_id)
[ "$T" -gt 0 ] 2>/dev/null && ok "create thread ($T)" || bad "create thread" "$T"
eq "thread info group"         "$(curl -s $A/threads/$T | jq -r .group_id)" "$G"
eq "role groups lists thread"  "$(curl -s $A/roles/$ME/groups | jq -c "[.groups[] | select(.group_id==$G) | .threads[] | select(.thread_id==$T) | .thread_id]")" "[$T]"
eq "role groups unknown role 404" "$(code $A/roles/totally-unregistered-role/groups)" 404
eq "lookup by topic"           "$(curl -s -G $A/groups/$G/threads/lookup --data-urlencode 'topic=QA run (coordinator-claude only, auto-deleted)' | jq -r .thread_id)" "$T"
eq "post to missing thread 404" "$(code -X POST $A/threads/999999/messages -H 'Content-Type: application/json' -d '{"sender_role":"coordinator-claude","body":"x"}')" 404
eq "unregistered sender 404"    "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"totally-unregistered-role","body":"x"}')" 404
eq "unregistered bind 404"      "$(code -X POST $A/roles/totally-unregistered-role/bind -H 'Content-Type: application/json' -d '{"session_name":"x"}')" 404
eq "unregistered group member 404" "$(code -X POST $A/groups -H 'Content-Type: application/json' -d '{"members":["coordinator-claude","totally-unregistered-role"]}')" 404
eq "non-member sender 403"     "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"architect-claude","body":"x"}')" 403
eq "history for missing thread 404" "$(code $A/threads/999999/history)" 404
eq "members for missing group 404" "$(code $A/groups/999999/members)" 404
eq "delivered for missing message 404" "$(code -X POST "$A/messages/999999/delivered?role=$ME")" 404

echo "== CSRF guard (security review finding, approved 2026-09-28)"
# Runs after G exists (get-or-create above), so "own Origin passes" below is
# an idempotent no-op regardless of fresh-DB state -- it must NOT be the
# first thing to create the {$ME} group, or it silently steals coverage of
# the "one-member group created (existing=false)" branch above forever
# (code review finding, 2026-09-28).
eq "missing X-Relay-Client header 403" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A/groups" -H 'Content-Type: application/json' -d "{\"members\":[\"$ME\"]}")" 403
eq "foreign Origin 403" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A/groups" -H 'Content-Type: application/json' -H 'X-Relay-Client: qa' -H 'Origin: https://evil.example' -d "{\"members\":[\"$ME\"]}")" 403
eq "own Origin passes"  \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A/groups" -H 'Content-Type: application/json' -H 'X-Relay-Client: qa' -H "Origin: $A" -d "{\"members\":[\"$ME\"]}")" 200
eq "wrong Host 400" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A/groups" -H 'Content-Type: application/json' -H 'X-Relay-Client: qa' -H 'Host: evil.example' -d "{\"members\":[\"$ME\"]}")" 400
eq "GET needs no X-Relay-Client" "$(curl -s -o /dev/null -w '%{http_code}' "$A/health")" 200
eq "GET still needs the right Host" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$A/health")" 400

echo "== posting + needs_operator"
R=$(post /threads/$T/messages '{"sender_role":"operator","body":"QA plain message from the operator."}')
M1=$(echo "$R" | jq -r .message_id)
eq "operator's post reaches the member" "$(echo "$R" | jq -c .recipients)" "[\"$ME\"]"
eq "post returns group_id"     "$(echo "$R" | jq -r .group_id)" "$G"
eq "operator never gets a delivery row" "$(db "SELECT count(*) FROM deliveries WHERE recipient_role='operator'")" 0
eq "bad kind 422"              "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"coordinator-claude","body":"x","needs_operator":{"kind":"urgent","why":"x"}}')" 422
eq "empty why 422"             "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"coordinator-claude","body":"x","needs_operator":{"kind":"bug","why":""}}')" 422
LONG=$(printf 'w%.0s' $(seq 1 201))
eq "201-char why 422"          "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d "{\"sender_role\":\"coordinator-claude\",\"body\":\"x\",\"needs_operator\":{\"kind\":\"bug\",\"why\":\"$LONG\"}}")" 422
OVERSIZE=$(head -c 50001 /dev/zero | tr '\0' 'w')
OVERSIZE_JSON=$(jq -n --arg b "$OVERSIZE" '{sender_role:"coordinator-claude", body:$b}')
eq "oversize body 422"         "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d "$OVERSIZE_JSON")" 422
ASK1=$(post /threads/$T/messages '{"sender_role":"coordinator-claude","body":"coordinator-claude -> operator: QA ask 1 (to be answered).","needs_operator":{"kind":"decision","why":"QA: answer this one"}}' | jq -r .message_id)
eq "last action on post"       "$(db "SELECT last_seen_action FROM roles WHERE role='$ME'")" "posted #$ASK1"
ASK2=$(post /threads/$T/messages '{"sender_role":"coordinator-claude","body":"coordinator-claude -> operator: QA ask 2 (to be cleared).","needs_operator":{"kind":"security","why":"QA: clear this one"}}' | jq -r .message_id)
ASK3=$(post /threads/$T/messages '{"sender_role":"coordinator-claude","body":"coordinator-claude -> operator: QA ask 3, left open.","needs_operator":{"kind":"approval","why":"QA: approve PR #999 (UI test)"}}' | jq -r .message_id)
eq "GET /attention lists 3 open" "$(curl -s $A/attention | jq -c "[.open[] | select(.thread_id==$T) | .message_id]")" "[$ASK1,$ASK2,$ASK3]"

echo "== answers / clear"
OTHER_T=$(post /groups/$G/threads '{"topic":"QA throwaway thread for cross-thread answers check"}' | jq -r .thread_id)
OTHER_M=$(post /threads/$OTHER_T/messages '{"sender_role":"coordinator-claude","body":"QA: message in a different thread."}' | jq -r .message_id)
eq "answers other thread 422"  "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d "{\"sender_role\":\"operator\",\"body\":\"x\",\"answers\":$OTHER_M}")" 422
eq "answers missing msg 422"   "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"operator","body":"x","answers":99999999}')" 422
eq "non-operator answers 403"  "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d "{\"sender_role\":\"coordinator-claude\",\"body\":\"x\",\"answers\":$ASK1}")" 403
AN=$(post /threads/$T/messages "{\"sender_role\":\"operator\",\"body\":\"QA answer to $ASK1.\",\"answers\":$ASK1}" | jq -r .message_id)
eq "answered_by recorded"      "$(db "SELECT attn_answered_by FROM messages WHERE message_id=$ASK1")" "$AN"
eq "answer to non-ask is harmless" "$(post /threads/$T/messages "{\"sender_role\":\"operator\",\"body\":\"QA reply to plain.\",\"answers\":$M1}" | jq -r 'has("message_id")')" true
eq "plain msg stays unflagged" "$(db "SELECT attn_kind IS NULL AND attn_answered_by IS NULL FROM messages WHERE message_id=$M1")" t
eq "clear ask2"                "$(post /messages/$ASK2/attention/clear '{}')" '{"ok":true}'
eq "clear ask2 again 404"      "$(code -X POST $A/messages/$ASK2/attention/clear)" 404
eq "clear non-flagged 404"     "$(code -X POST $A/messages/$M1/attention/clear)" 404
eq "only ask3 open"            "$(curl -s $A/attention | jq -c "[.open[] | select(.thread_id==$T) | .message_id]")" "[$ASK3]"

echo "== read / ack / pending"
eq "pending = operator's 3 posts"  "$(mine | jq length)" 3
eq "after=M1 filters"          "$(curl -s "$A/roles/$ME/pending?after=$M1&peek=true" | jq "[.pending[].message_id] | all(. > $M1)")" true
eq "pending has created_at + delivered_at" "$(curl -s "$A/roles/$ME/pending?peek=true" | jq -r '.pending[0] | has("created_at") and has("delivered_at")')" true
SEEN0=$(db "SELECT last_seen_at FROM roles WHERE role='$ME'")
curl -s "$A/roles/$ME/pending?peek=true" >/dev/null
eq "peek leaves last_seen alone" "$(db "SELECT last_seen_at FROM roles WHERE role='$ME'")" "$SEEN0"
curl -s "$A/roles/$ME/pending" >/dev/null
eq "own pending records 'checked mail'" "$(db "SELECT last_seen_action FROM roles WHERE role='$ME'" | sed 's/ (.*//')" "checked mail"
eq "read M1"                   "$(post "/messages/$M1/read?role=$ME" '{}')" '{"ok":true}'
eq "re-read M1 409"            "$(code -X POST "$A/messages/$M1/read?role=$ME")" 409
eq "last action on read"       "$(db "SELECT last_seen_action FROM roles WHERE role='$ME'")" "read #$M1"
eq "ack AN"                    "$(post "/messages/$AN/ack?role=$ME" '{}')" '{"ok":true}'
eq "ack sets read too"         "$(db "SELECT read_at IS NOT NULL AND ack_at IS NOT NULL FROM deliveries WHERE message_id=$AN AND recipient_role='$ME'")" t
eq "re-ack AN 409"             "$(code -X POST "$A/messages/$AN/ack?role=$ME")" 409
eq "history acked_by"          "$(curl -s $A/threads/$T/history | jq -c ".messages[] | select(.message_id==$AN) | .acked_by")" "[\"$ME\"]"
eq "pending drops read/acked"  "$(mine | jq length)" 1
eq "read by non-recipient 404" "$(code -X POST "$A/messages/$M1/read?role=observability-claude")" 404
eq "read missing message 404"  "$(code -X POST "$A/messages/99999999/read?role=$ME")" 404
eq "ack by non-recipient 404"  "$(code -X POST "$A/messages/$M1/ack?role=observability-claude")" 404
eq "404s don't touch observability-claude"     "$(db "SELECT coalesce(last_seen_action,'') NOT LIKE '%$M1%' FROM roles WHERE role='observability-claude'")" t
eq "role in body 422"          "$(code -X POST $A/messages/$M1/read -H 'Content-Type: application/json' -d "{\"role\":\"$ME\"}")" 422

echo "== delivered (WhatsApp-style)"
LEFT=$(mine | jq '.[0]')
B0=$(db "SELECT last_seen_at FROM roles WHERE role='$ME'")
eq "delivered-batch"           "$(post /messages/$LEFT/delivered-batch "{\"roles\":[\"$ME\"]}" | jq -c .marked)" "[\"$ME\"]"
eq "delivered isn't activity"  "$(db "SELECT last_seen_at FROM roles WHERE role='$ME'")" "$B0"
D0=$(db "SELECT delivered_at FROM deliveries WHERE message_id=$LEFT AND recipient_role='$ME'")
post "/messages/$LEFT/delivered?role=$ME" '{}' >/dev/null
eq "first delivered_at is kept" "$(db "SELECT delivered_at FROM deliveries WHERE message_id=$LEFT AND recipient_role='$ME'")" "$D0"

echo "== dashboard state"
S=$(curl -s $A/api/dashboard/state)
eq "state keys"                "$(echo "$S" | jq -c 'keys')" '["attention","deliveries","groups","message_counts","messages","roles","server_time","threads"]'
eq "roles carry last_seen + heartbeat" "$(echo "$S" | jq -r '.roles[0] | has("last_seen_at") and has("last_action_at") and has("heartbeat_at")')" true
eq "operator not in roles list"    "$(echo "$S" | jq '[.roles[] | select(.role=="operator")] | length')" 0
eq "open ask3 in attention"    "$(echo "$S" | jq -r ".attention[] | select(.message_id==$ASK3) | (.attn_answered_by==null and .attn_cleared_at==null)")" true
TS=$(echo "$S" | jq -r .server_time); LAST=$(echo "$S" | jq '[.messages[].message_id]|max')
I=$(curl -s -G $A/api/dashboard/state --data-urlencode "since_id=$LAST" --data-urlencode "since_ts=$TS")
eq "incremental: nothing new"  "$(echo "$I" | jq '.messages|length')" 0
sleep 11   # past the relay's deliberate 10 s since_ts overlap
TSL=$(curl -s "$A/api/dashboard/state?since_id=999999999" | jq -r .server_time)
eq "incremental: answered asks not resent (after overlap)" "$(curl -s -G $A/api/dashboard/state --data-urlencode "since_id=$LAST" --data-urlencode "since_ts=$TSL" | jq "[.attention[] | select(.message_id==$ASK1)] | length")" 0
M5=$(post /threads/$T/messages '{"sender_role":"operator","body":"QA incremental."}' | jq -r .message_id)
I=$(curl -s -G $A/api/dashboard/state --data-urlencode "since_id=$LAST" --data-urlencode "since_ts=$TS")
eq "incremental: exactly the new msg" "$(echo "$I" | jq -c '[.messages[].message_id]')" "[$M5]"
eq "incremental: its delivery" "$(echo "$I" | jq -c "[.deliveries[] | select(.[0]==$M5)]")" "[[$M5,\"$ME\",\"p\"]]"
post "/messages/$M5/ack?role=$ME" '{}' >/dev/null
I2=$(curl -s -G $A/api/dashboard/state --data-urlencode "since_id=$M5" --data-urlencode "since_ts=$TS")
eq "incremental: ack change seen" "$(echo "$I2" | jq -c "[.deliveries[] | select(.[0]==$M5)]")" "[[$M5,\"$ME\",\"a\"]]"

echo "== heartbeat"
eq "heartbeat bad interval 422" "$(code -X POST "$A/roles/$ME/heartbeat?interval=0")" 422
ACT0=$(db "SELECT last_action_at||' '||last_seen_action FROM roles WHERE role='$ME'")
eq "heartbeat ok"              "$(post "/roles/$ME/heartbeat?interval=20" '{}')" '{"ok":true}'
eq "heartbeat sets heartbeat_at/interval/last_seen" "$(db "SELECT heartbeat_at > now() - interval '5 seconds' AND heartbeat_interval = 20 AND last_seen_at = heartbeat_at FROM roles WHERE role='$ME'")" t
eq "heartbeat leaves last action" "$(db "SELECT last_action_at||' '||last_seen_action FROM roles WHERE role='$ME'")" "$ACT0"

echo "== mailwatch.sh"
NOW=$(db "SELECT max(message_id) FROM messages")
# Anything already unread for me has reached me (I'm the one running this),
# so mark it delivered; otherwise real mail would (correctly) wake the watch.
for id in $(curl -s "$A/roles/$ME/pending?peek=true" | jq -r '.pending[] | select(.delivered_at == null) | .message_id'); do
  post "/messages/$id/delivered?role=$ME" '{}' >/dev/null; done
timeout 6 $W $ME $NOW 2 >/tmp/qa_watch1; eq "delivered unread mail <= after doesn't wake (no loop)" "$?" 124
( sleep 3; post /threads/$T/messages '{"sender_role":"operator","body":"QA mailwatch trigger."}' >/tmp/qa_trig ) &
timeout 20 $W $ME $NOW 2 >/tmp/qa_watch2; RC=$?; wait
MW=$(jq -r .message_id /tmp/qa_trig)
eq "wakes on new mail (exit 0)" "$RC" 0
grep -q "msg $MW in thread $T from operator" /tmp/qa_watch2 && ok "prints the new msg id" || bad "prints the new msg id" "$(cat /tmp/qa_watch2)"
grep -q "$ME $MW\$" /tmp/qa_watch2 && ok "prints concrete re-arm id" || bad "prints concrete re-arm id" "$(tail -1 /tmp/qa_watch2)"
eq "marks it delivered, not read" "$(db "SELECT delivered_at IS NOT NULL AND read_at IS NULL FROM deliveries WHERE message_id=$MW AND recipient_role='$ME'")" t
UND=$(post /threads/$T/messages '{"sender_role":"operator","body":"QA undelivered, below the re-arm id."}' | jq -r .message_id)
FUT=$(( UND + 1000 ))
timeout 8 $W $ME $FUT 2 >/tmp/qa_watch4; RC=$?
eq "undelivered mail <= after still wakes (msg 746 hazard)" "$RC" 0
grep -q "msg $UND " /tmp/qa_watch4 && ok "  ...and names it" || bad "  ...and names it" "$(cat /tmp/qa_watch4)"
( sleep 3; post /threads/$T/messages '{"sender_role":"operator","body":"QA late-lower-id trigger."}' >/tmp/qa_trig2 ) &
timeout 20 $W $ME $FUT 2 >/tmp/qa_watch3; RC=$?; wait
eq "id <= after that appears later wakes" "$RC" 0
grep -q "msg $(jq -r .message_id /tmp/qa_trig2) " /tmp/qa_watch3 && ok "  ...and names it" || bad "  ...and names it" "$(cat /tmp/qa_watch3)"
HB0=$(db "SELECT heartbeat_at FROM roles WHERE role='$ME'")
sleep 1; timeout 4 $W $ME 999999999 1 >/dev/null 2>&1
eq "watch loop sends heartbeats" "$(db "SELECT heartbeat_at > '$HB0' AND heartbeat_interval = 1 FROM roles WHERE role='$ME'")" t
post "/roles/$ME/heartbeat?interval=20" '{}' >/dev/null
RELAY_URL=http://127.0.0.1:1 timeout 5 $W $ME 0 1 >/dev/null 2>&1; eq "keeps waiting when relay down" "$?" 124
eq "usage error"               "$($W 2>&1 >/dev/null | grep -c usage)" 1
$W $ME '<highest message_id you handled>' >/dev/null 2>&1; eq "non-numeric after -> exit 2" "$?" 2
$W nonexistent-role 1 1 >/dev/null 2>&1; eq "never-bound role (nonexistent-role) -> exit 2" "$?" 2

echo "== archive"
eq "archive"                   "$(post /threads/$T/archive '{}')" '{"ok":true}'
eq "role groups drops archived thread" "$(curl -s $A/roles/$ME/groups | jq -c "[.groups[] | select(.group_id==$G) | .threads[] | select(.thread_id==$T)] | length")" 0
eq "role groups keeps empty group" "$(curl -s $A/roles/$ME/groups | jq -c "[.groups[] | select(.group_id==$G)] | length")" 1
eq "post to archived 409"      "$(code -X POST $A/threads/$T/messages -H 'Content-Type: application/json' -d '{"sender_role":"coordinator-claude","body":"x"}')" 409
eq "history still readable"    "$(curl -s $A/threads/$T/history | jq '.messages|length>0')" true
T_REOPEN=$(post /groups/$G/threads '{"topic":"QA run (coordinator-claude only, auto-deleted)"}' | jq -r .thread_id)
eq "lookup prefers active over archived, same topic" \
  "$(curl -s -G $A/groups/$G/threads/lookup --data-urlencode 'topic=QA run (coordinator-claude only, auto-deleted)' | jq -r .thread_id)" "$T_REOPEN"

echo "== 1000-message cap (first-load /api/dashboard/state)"
# A direct SQL bulk-insert, not 1000+ real posts, to keep this suite fast.
# Uses its own throwaway group (not $G) so it can be fully deleted
# afterward -- otherwise the filler messages would shift every later
# message_id and break the UI fixture's #$M1-style in-body references.
CAP_G=$(post /groups '{"members":["coordinator-claude","architect-claude"]}' | jq -r .group_id)
CAP_T=$(post /groups/$CAP_G/threads '{"topic":"QA message-cap thread (auto-deleted)"}' | jq -r .thread_id)
CAP_ASK=$(post /threads/$CAP_T/messages '{"sender_role":"coordinator-claude","body":"QA: old open ask that must survive the cap.","needs_operator":{"kind":"decision","why":"QA: cap coverage"}}' | jq -r .message_id)
db "INSERT INTO messages (thread_id, sender_role, body) SELECT $CAP_T, 'coordinator-claude', 'QA cap filler ' || g FROM generate_series(1, 1005) g" >/dev/null
STATE=$(curl -s "$A/api/dashboard/state")
eq "old open ask's message is still loaded past the cap" \
  "$(echo "$STATE" | jq "[.messages[].message_id] | index($CAP_ASK) != null")" true
eq "old open ask still in attention" \
  "$(echo "$STATE" | jq "[.attention[].message_id] | index($CAP_ASK) != null")" true
eq "message_counts reflects the true total, not the loaded count" \
  "$(echo "$STATE" | jq ".message_counts[\"$CAP_T\"] >= 1006")" true
eq "deliveries never reference a message outside the loaded set" \
  "$(echo "$STATE" | jq '[.messages[].message_id] as $loaded | all(.deliveries[]; .[0] as $mid | $loaded | index($mid) != null)')" true
eq "old open ask's own delivery is still present" \
  "$(echo "$STATE" | jq "[.deliveries[] | select(.[0]==$CAP_ASK)] | length > 0")" true
curl -s -o /dev/null -X DELETE -H 'X-Relay-Client: qa' "$A/groups/$CAP_G"

echo "== UI fixture"
T2=$(post /groups/$G/threads '{"topic":"QA UI run (coordinator-claude only, auto-deleted)"}' | jq -r .thread_id)
UIASK=$(post /threads/$T2/messages "{\"sender_role\":\"coordinator-claude\",\"body\":\"coordinator-claude -> operator: QA UI ask. Mentions \`code\`, PR #999, sha 1a2b3c4d, message #$M1 and thread $T.\",\"needs_operator\":{\"kind\":\"approval\",\"why\":\"QA UI: approve PR #999\"}}" | jq -r .message_id)
MDBODY=$(jq -n --arg m1 "$M1" '"coordinator-claude -> operator: **Markdown QA** with a [PR link](https://github.com/example/example-repo/pull/1) and a bare https://example.com/plan?x=1.\n\n## Plan review\n\n- first *item* with `inline code`\n- second item, see #" + $m1 + "\n\n1. step one\n2. step two\n\n> a quote\n\n```\n$ kubectl get pods\npod-1 Running\n```\n\nUnsafe: [click](javascript:alert(1)) <script>alert(2)</script> <b>raw</b>\n\nTricky: [bs](/\\evil.example) [dbl](//evil.example) [enc](jav&#x61;script:alert(3)) [ok](/dashboard/threads/1)"')
MD=$(post /threads/$T2/messages "{\"sender_role\":\"coordinator-claude\",\"body\":$MDBODY}" | jq -r .message_id)
echo "G=$G T=$T T2=$T2 UIASK=$UIASK M1=$M1 ASK3=$ASK3 MD=$MD" > "$DIR/fixture.env"
echo
echo "API QA: $PASS passed, $FAIL failed   (fixture: $(cat "$DIR/fixture.env"))"
echo "Ran against $A -- a REAL database, not a sandbox. Leaves group \$G=$G behind for ui_qa.mjs;"
echo "clean up afterward: curl -s -X DELETE -H 'X-Relay-Client: qa' $A/groups/$G"
[ "$FAIL" -eq 0 ]
