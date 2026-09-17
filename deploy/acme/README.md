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

Verify that rather than trusting it. With the key configured, try to change
something else and confirm `AccessDenied`:

```
aws route53 change-resource-record-sets --hosted-zone-id <ZONE> \
  --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":
    {"Name":"probe.ballaban.ca","Type":"A","TTL":60,
     "ResourceRecords":[{"Value":"192.0.2.1"}]}}]}'
```

If that succeeds, the policy is not doing what this file claims.

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
5. **Choose the CA.** `acme.sh` defaults to ZeroSSL, which wants a registration
   step; Let's Encrypt is what everything else here already trusts:

   ```
   docker exec acme acme.sh --set-default-ca --server letsencrypt
   ```

6. **Issue it:**

   ```
   docker exec acme acme.sh --issue --dns dns_aws -d '*.ballaban.ca'
   ```

   Watch it write `_acme-challenge.ballaban.ca`, wait for the change, and clean
   up after itself. A wildcard's challenge is at the bare name — there is no
   `_acme-challenge.*.ballaban.ca`.

7. **Install it into DSM:**

   ```
   docker exec acme acme.sh --deploy -d '*.ballaban.ca' --deploy-hook synology_dsm
   ```

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
