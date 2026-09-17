# A wildcard certificate that renews itself

One certificate for `*.ballaban.ca`, issued by proving control of the domain
through **DNS** rather than through a web server, and installed into DSM by its
own API.

Two files here, and they are worth reading before anything is created:

- [`route53-policy.json`](route53-policy.json) — what the AWS credential may do.
- [`docker-compose.yml`](docker-compose.yml) — where it runs and what it is told.

## Why, in one paragraph

`pix.ballaban.ca` has a public DNS record for exactly one reason: Let's Encrypt
validates it by fetching a file over port 80, so the name has to resolve
publicly and the port has to be open. That public record is also what collides
with the router's local override — a name cannot hold a CNAME and an A record at
once, and clients that receive both improvise, which is how a phone ends up on
the router's self-signed certificate. DNS-01 needs neither the record nor the
port, so the record can be deleted and the collision disappears.

## What it does not touch

The credential is scoped by IAM **condition keys** to TXT records whose
normalized name matches `_acme-challenge.*`. It cannot change an MX record, an A
record, a CNAME, or a TXT record at the apex — so Proton Mail's delivery, SPF,
DKIM and DMARC are out of its reach by construction rather than by convention.

Verify that rather than trusting it. In a terminal of your own — not one that
records what you type — with the key in the environment:

```powershell
$env:AWS_ACCESS_KEY_ID = 'AKIA...'
$env:AWS_SECRET_ACCESS_KEY = '...'
$env:AWS_DEFAULT_REGION = 'us-east-1'   # Route 53 is global; this is convention

# Should succeed: the credential can read the zone.
aws route53 list-resource-record-sets `
  --hosted-zone-id Z03980672KUERFJORTYH3 --max-items 3

# Should fail with AccessDenied: it may write nothing but a challenge.
aws route53 change-resource-record-sets `
  --hosted-zone-id Z03980672KUERFJORTYH3 `
  --change-batch file://deploy/acme/deny-probe.json
```

The first proves the credential works at all. Without it a refusal on the
second proves nothing, because a scoped policy and a mistyped key give the same
kind of error. If the **second** succeeds, the policy is not doing what this
file claims, and nothing should be deployed until it does.

[`deny-probe.json`](deny-probe.json) writes an ordinary A record at a name
nothing uses, pointing at `192.0.2.1` — a documentation address that routes
nowhere. Harmless in the one case where it matters, which is the case where the
policy failed.

## Steps

1. **Find the hosted zone id** — Route 53 → Hosted zones → `ballaban.ca`. It
   looks like `Z0123456789ABCDEFGHIJ`. Put it in both places in
   `route53-policy.json`, replacing `REPLACE_WITH_ZONE_ID`.
2. **Create the IAM user** with no console access and that policy attached, and
   keep its access key. Run the probe above; expect it to be refused.
3. **Create a DSM account** in the administrators group, used for nothing else,
   with a password used nowhere else.
4. **Start the container** with the four real values in Container Manager's
   environment fields.
   Verified against the image rather than assumed: `acme.sh` lives at
   `/usr/local/bin/acme.sh`, its hooks are baked in at `/acmebin/dnsapi/dns_aws.sh`
   and `/acmebin/deploy/synology_dsm.sh`, and `LE_CONFIG_HOME` is `/acme.sh` —
   which is what the volume above mounts, and therefore the only state worth
   keeping. `daemon` runs supercronic against a crontab it writes on first
   start, checking four times a day and renewing at sixty.

5. **Open a shell — as `sh`.** The image is Alpine and has **no bash**, while
   Container Manager's terminal launches `bash` by default. The symptom is a
   session that opens and sits there saying nothing, which reads as a hung
   command rather than a missing shell. Set the launch command to `sh`.

   With no SSH on the NAS this is the only exec path, so the fallback is worth
   knowing: the entrypoint runs whatever arguments it is given and treats only
   `daemon` specially, so a command can be run by temporarily replacing the
   container's execution command and reading the Log tab.

6. **Issue it:**

   ```
   acme.sh --issue --dns dns_aws -d '*.ballaban.ca' --server letsencrypt
   ```

   `--server letsencrypt` per-certificate rather than `--set-default-ca`: one
   command instead of two, and it records the CA in the certificate's own
   config where a later reader can see it. Without it you get ZeroSSL, which is
   acme.sh's default and nobody's intention here.

   Watch it write `_acme-challenge.ballaban.ca`, wait for the change, and clean
   up after itself. A wildcard's challenge is at the bare name — there is no
   `_acme-challenge.*.ballaban.ca`.

7. **Install it into DSM, as a separate command:**

   ```
   acme.sh --deploy -d '*.ballaban.ca' --deploy-hook synology_dsm
   ```

   **Not as `--deploy-hook` on the issue above**, which is the obvious way to
   save a step and does not work: issuance reports success, the hook is never
   recorded, and DSM goes on serving the old certificate while every message on
   screen says the thing worked. Check `Le_DeployHook` in the certificate's
   `.conf` afterwards — if it is absent, renewals will issue and never install,
   which is a failure that waits sixty days to appear.

7b. **Assign it.** The hook imports the certificate; it does not put it into
   service, and a newly created one starts unassigned. Control Panel → Security
   → Certificate → **Settings**, and point every service at it. Until then DSM
   holds the new certificate and serves the old one.

8. **Verify before removing anything.** DSM holds several certificates at once,
   so the old one is still there and this is reversible until step 10:

   ```
   Control Panel → Security → Certificate      the new one is present
   Control Panel → Security → Certificate → Settings   each service uses it
   ```

   Then from a machine on the LAN, for every name that matters:

   ```
   curl -sv https://pix.ballaban.ca/healthz 2>&1 | grep -E 'subject|issuer'
   ```

9. **Point the router's console at it too.** UniFi OS can import a certificate,
   and the wildcard covers `ui.ballaban.ca` — which the current five-name
   certificate does not. That ends the last self-signed warning in the house.

10. **Delete the public `pix.ballaban.ca` record** in Route 53. This is the step
    that fixes the original problem: with nothing upstream, the router's local
    answer is the only answer in existence.

11. **Fix the local override.** With the public CNAME gone, a single local
    A record for `pix.ballaban.ca` → `192.168.1.140` is now correct and
    unambiguous. Until step 10 it is neither.

12. **Close the port-80 forward.** Nothing needs it any more.

12b. **Rehearse the renewal, before trusting the CAA records.** A wrong CAA
    does not fail now; it fails at renewal, silently, and is discovered by the
    whole household at once. Force one while you are watching:

    ```
    acme.sh --renew -d '*.ballaban.ca' --force
    ```

    It exercises everything in one go — the IAM policy writing the challenge,
    Let's Encrypt checking CAA against the account, the deploy hook reinstalling
    the result. `Le_NextRenewTimeStr` moving is the proof that a certificate was
    issued *after* the CAA existed. Five duplicate certificates a week are
    allowed, so this costs nothing.

13. **Add an expiry check.** A manual renewal fails loudly, because you are
    standing there; an automatic one fails quietly and is discovered ninety days
    later by everybody at once. Whatever form it takes, it should say how many
    days are left and complain below about twenty.

## What changes about the old certificate

DSM's built-in Let's Encrypt integration stops being the owner of these names.
Two systems renewing the same domain would fight, so once the wildcard is
serving, remove the five-name certificate rather than leaving it to renew
against a port that is no longer open.

## What this deliberately does not do

It does not make anything public. `pix.ballaban.ca` ends up resolving only
inside the network, which is where it already resolved for every device that was
working. Making it public later is one Route 53 record and no certificate work
at all — the wildcard already covers it — plus the exposure question, which is a
separate decision and a larger one.
