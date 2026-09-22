# Certificate renewal and expiry

`control.certificates` renews leaf certificates under the **existing** authority.
It never replaces `ca.pem` or `ca.key`, copies files to workers, or starts/stops
services. A new authority is a separate trust migration, not a renewal.

`status(tls_dir, db)` returns public subject, role, fingerprint, expiry and days
remaining for the authority, active controller/receiver pair, and pinned worker
certificates. Warnings start 30 days before expiry. Missing or invalid public
certificates are reported as unavailable. Status never reads a private key.
Run it daily and alert on any non-null `warning`; do not wait for TLS failures.

The CLI exposes the same guarded operations (paths default to the deployment):

```sh
python -m control certificates status
python -m control certificates prepare controller
python -m control certificates prepare agent --subject rnr-linux-1
# Only after maintenance and both consumers have actually stopped:
python -m control certificates activate controller /data/control-tls/renewals/controller-EXAMPLE --services-stopped
python -m control certificates rollback controller --services-stopped
# Only after this agent stopped and the prepared files were installed:
python -m control certificates activate agent /data/control-tls/renewals/agent-EXAMPLE --subject rnr-linux-1 --agent-stopped --bundle-installed
```

Use `certificates --tls-dir PATH --db PATH <command>` for explicit locations.

## Prepare

Use `prepare(tls_dir, role, subject, db, days=365)` from the controller host.
Roles are `agent`, `controller` and `receiver`. For an agent, subject is its
enrolled host ID; controller and receiver use `controller`.

Each preparation writes a unique directory under `control-tls/renewals`, with
directory mode 0700 and files mode 0600 on POSIX. On Windows additionally retain
the TLS directory's restricted NTFS ACL; POSIX mode bits are not an NTFS ACL.
The bundle contains the public CA certificate, the new leaf/key pair and public
renewal metadata. **It contains no CA private key.** Preparation changes no live
pin or active certificate. Keep this directory private, including in backups.
Leaf expiry is bounded by the CA expiry. A CA expiry warning requires planning
a separate trust migration; leaf renewal cannot extend the authority's lifetime.

## Rotate one worker

1. Enable platform maintenance explicitly. Maintenance pauses reconciliation;
   it does not stop runner listeners or active CI jobs. First finish or safely
   stop work using the runner shutdown runbook, then stop the worker agent.
2. Retain the previous worker files and its pinned fingerprint for recovery.
   Copy only `agent.crt`, `agent.key` and `ca.pem` from the prepared bundle into
   the worker's configured TLS directory, preserving its restricted permissions.
   Check the copied public certificate's fingerprint against the prepared result.
3. Call `activate_worker(tls_dir, bundle, host_id, db, agent_stopped=True,
   bundle_installed=True)`. These flags explicitly attest to steps already
   completed; they do not perform them. The helper validates the authority,
   signature, subject, SAN, validity, EKU and certificate/key match before updating
   the pin. It refuses a bundle prepared against an older pin. Repeating activation
   of the same current bundle is harmless.
4. Restart that agent. Confirm an authenticated heartbeat and a controller read
   against the worker. Only then restore normal reconciliation. Keep prior
   credentials private until recovery is no longer needed.

If copying failed before activation, restore the old worker files; its old pin
is unchanged. If the new bundle was pinned but the worker fails to start, keep
maintenance enabled and repair the installed pair/configuration. Do not restore
an old certificate while leaving the new pin, and do not use re-enrolment as a
shortcut: it also changes registration metadata and policy.

Replacing a pin rejects the old agent certificate at the controller. This is
**pin replacement**, not a CA-wide CRL or OCSP revocation service.

## Rotate controller or receiver

1. Enable maintenance, prepare the replacement, and stop both controller and
   dashboard TLS consumers. Retain the old files and prepared bundle.
2. Call `activate_local(tls_dir, bundle, role, db, services_stopped=True)`.
   It atomically replaces `active-certificates.json`, selecting both files in
   one immutable bundle. It never overwrites one half of a live pair.
3. All TLS consumers must call `resolve_paths(tls_dir, role)` **once per TLS
   context**, obtaining certificate, key and CA paths from the same manifest
   snapshot. Existing filenames remain the fallback until first activation.
4. Restart controller and dashboard, check receiver heartbeats and controller
   agent reads, then disable maintenance when normal operation is appropriate.

To roll back, retain maintenance and stop both consumers, then call
`rollback_local(tls_dir, role, db, services_stopped=True)`. The previous pair is
validated again and selected with one atomic manifest replacement. An expired
old certificate cannot be restored by this helper. Restart and verify again.
On a process crash, the manifest points to either the old complete pair or the
new complete pair; immutable bundle files remain available for diagnosis.

Cryptographic validation uses the installed `cryptography` X.509 implementation.
Issuer/signature verification is supplemented with validity, CA constraints,
role-specific EKU, identity, SAN and key-pair checks, as required by the
[X.509 reference](https://cryptography.io/en/latest/x509/reference/#cryptography.x509.Certificate.verify_directly_issued_by).
