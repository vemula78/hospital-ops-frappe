# Operating notes — hospital_ops on erp.sssihms.org

## After `install-app` or any code deploy: restart the web workers

`bench --site frontend migrate` + `clear-cache` is **not enough**. The gunicorn
workers in the `backend` container are long-lived processes; an app installed
(or a new editable install changed) after they started is invisible to them —
Python reads the editable-install `.pth` at interpreter startup only. The
symptom is total: every HTTP request 500s with
`ModuleNotFoundError: No module named 'hospital_ops'`, while every
`bench console` / `bench execute` check passes, because those spawn fresh
interpreters. This happened on 24-Aug-2026: the public ERP URL served 500s for
the entire Phase 1/2 build session and no console-based verification caught it.

The restart, in this exact order (frontend's nginx caches backend's container
IP — restarting backend alone causes 502s):

```bash
cd ~/frappe_docker
sudo docker compose -f pwd.yml restart backend
sudo docker compose -f pwd.yml restart frontend queue-short queue-long scheduler websocket
curl -s https://erp.sssihms.org/api/method/ping   # expect {"message":"pong"}
```

Never `docker compose down`, never reboot the VM — it also serves the
hospital's public WordPress site.

## Durability caveat (inherited from this instance's pattern)

The app lives in the `backend` container's writable layer (installed via
`bench new-app` / code copied in), like `sssihms_hr` and `facility_management`
before it. A container recreated from a fresh image pull loses it; the durable
copy is this repository. Reinstall path: `bench get-app
https://github.com/vemula78/hospital-ops-frappe --branch main` →
`bench --site frontend install-app hospital_ops` → restart as above.

## Copying code in: extract at BOTH directory levels

This install carries the app content at two levels, and both are load-bearing:

- `apps/hospital_ops/hospital_ops/` — the Python package. `hospital_ops.hooks`
  resolves from here, so `hooks.py`, `modules.txt`, `patches.txt` and
  `__init__.py` must exist at this level.
- `apps/hospital_ops/hospital_ops/hospital_ops/` — the Frappe *module*
  directory (it carries the `.frappe` marker). `hospital_ops.hospital_ops.*`
  resolves from here, and `frappe.get_module_path("Hospital Ops")` points here,
  so every doctype JSON, the reports and `tests_runner.py` must be at this
  level.

The repository is flat — `hospital_ops/hospital_ops/` holds both sets — so a
deploy extracts the same tarball twice:

```bash
tar czf /tmp/app.tgz --no-mac-metadata --exclude='__pycache__' \
  -C hospital_ops hospital_ops                      # archive root == that flat dir
scp /tmp/app.tgz azureuser@…:/tmp/app.tgz
sudo docker cp /tmp/app.tgz frappe_docker-backend-1:/tmp/app.tgz
sudo docker exec -u frappe frappe_docker-backend-1 sh -c \
  "cd /home/frappe/frappe-bench/apps/hospital_ops && tar xzf /tmp/app.tgz && \
   cd hospital_ops && tar xzf /tmp/app.tgz"
```

Extracting at only the deeper level fails `bench migrate` with
`ModuleNotFoundError: No module named 'hospital_ops.hooks'`; extracting at only
the shallower one leaves the new doctypes invisible to `migrate`, which then
reports success having synced nothing. Both symptoms were hit on 24-Aug-2026.
Use `--no-mac-metadata` (or delete `._*` afterwards) — macOS `tar` otherwise
scatters AppleDouble files through the app directory.

## Verification after any deploy

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase2_tests
```

32 invariant checks, all data rolled back regardless of outcome.

Phase 3 (CSR) has its own suite, 111 checks, same rollback-always shape:

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase3_tests
```

Phase 4 (Research) has its own suite, 32 checks, same shape, including the
§4.3 participant-identifier scan with its positive control:

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase4_tests
```

Phase 5 (Build & Publish: signage, website, software) has its own suite, 109
checks, same shape:

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase5_tests
```

Then check the public URL, not only the console — see above for why. The
four that matter after a restart: `/api/method/ping` returns pong, `/login`
returns 200, `/app/research-study` and `/app/hospital-sign` resolve 200
(following the redirect —
an unauthenticated request 301s to `/login` first, which is expected), and
`https://sssihms.org` (the hospital's public WordPress site, same VM) still
returns 200.
