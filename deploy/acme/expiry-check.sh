#!/bin/sh
# Is the certificate the NAS is serving about to expire?
#
# The one way automatic renewal is *worse* than the manual ritual it replaced:
# a ritual fails while you are standing there, and a cron job fails silently and
# is discovered ninety days later by everybody at once. This is what makes the
# automation strictly better rather than merely quieter.
#
# Deliberately checks what is **being served**, not what acme.sh has on disk.
# Those are different facts, and the gap between them is exactly the failure
# worth catching: a renewal that issued a new certificate and never installed it
# looks perfect from acme.sh's side.
#
# Run it from DSM's Task Scheduler, weekly, as root:
#
#   Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script
#     User:     root
#     Schedule: weekly
#     Command:  sh /volume1/pix2/app/acme/expiry-check.sh
#     Settings: tick "Send run details by email", and
#               "only when the script terminates abnormally"
#
# That last pair is the whole design: silence when it is fine, an email when it
# is not. A weekly "still fine" message is one you would filter within a month,
# and then not see the one that mattered.
#
# Exit 0 — more than DAYS left. Exit 1 — expiring, or could not be checked.

HOST=${1:-127.0.0.1}
PORT=${2:-443}
NAME=${3:-pix.ballaban.ca}
DAYS=${4:-21}

cert=$(echo | openssl s_client -connect "$HOST:$PORT" -servername "$NAME" 2>/dev/null)

if [ -z "$cert" ]; then
    echo "FAIL: nothing answered TLS on $HOST:$PORT"
    exit 1
fi

subject=$(echo "$cert" | openssl x509 -noout -subject 2>/dev/null)
enddate=$(echo "$cert" | openssl x509 -noout -enddate 2>/dev/null)

if [ -z "$enddate" ]; then
    echo "FAIL: $HOST:$PORT answered, but not with a certificate we could read"
    exit 1
fi

# `-checkend` rather than date arithmetic: no parsing, no locale, no busybox
# surprises. It returns non-zero when the certificate expires within the window.
if echo "$cert" | openssl x509 -noout -checkend $((DAYS * 86400)) >/dev/null 2>&1; then
    echo "ok: $subject"
    echo "    $enddate  (more than $DAYS days away)"
    exit 0
fi

echo "EXPIRING: $subject"
echo "    $enddate  — under $DAYS days away"
echo
echo "acme.sh renews at sixty days and installs through its deploy hook. Seeing"
echo "this means one of those did not happen. Check the acme container's log,"
echo "then: acme.sh --renew -d '*.ballaban.ca' --force"
exit 1
