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

## Copying code in: a single extract at the app root

**Superseded 25-Aug-2026.** This section previously told you to extract the same
tarball *twice* — once at `apps/hospital_ops/` and again at
`apps/hospital_ops/hospital_ops/`. That was a workaround for a packaging defect,
not a property of Frappe. The repository had one directory level too many, so the
double extract manufactured a duplicate of the whole package inside the module
directory, and `hospital_ops.hospital_ops.*` had been resolving to that duplicate
all along.

The repository layout is now correct — `doctype/`, `report/` and the helper
modules live in the module directory, which is where
`frappe.model.sync.sync_for` scans for doctypes. **Extract once, at the app root.**
Building the archive from anywhere other than the repository root produces a
near-empty tarball.

```bash
# on the Mac, from the repository root
git archive --format=tar --prefix=hospital_ops/ HEAD | gzip > /tmp/ho.tar.gz
scp -P 2222 -i ~/Downloads/sssihms-web-vm2023_key.pem /tmp/ho.tar.gz azureuser@20.219.253.136:/tmp/

# on the VM, for each of the five app containers
for c in backend queue-short queue-long scheduler websocket; do
  sudo docker cp /tmp/ho.tar.gz frappe_docker-$c-1:/tmp/
  sudo docker exec -u frappe -w /home/frappe/frappe-bench/apps frappe_docker-$c-1 sh -c \
    'rm -rf hospital_ops && tar -xzf /tmp/ho.tar.gz && \
     cd /home/frappe/frappe-bench && env/bin/pip install -q -e apps/hospital_ops'
done
```

Then `bench --site <site> migrate --skip-search-index`, and restart in the order
above. Confirm afterwards that no duplicate remains:

```bash
sudo docker exec frappe_docker-backend-1 sh -c \
  'test -d /home/frappe/frappe-bench/apps/hospital_ops/hospital_ops/hospital_ops/hospital_ops \
   && echo "DUPLICATE PRESENT" || echo clean'
```

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

Phase 5 (Build & Publish: signage, website, software) has its own suite, 142
checks, same shape:

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase5_tests
```

Phase 6 (Weekly Review, dashboard/notification fixtures, the over-receipt
hook) has its own suite, 75 checks, same shape. It also expects the
dashboard/notification fixture records to already exist — a fresh
`bench migrate` installs them from `hospital_ops/fixtures/*.json`, but on a
site that has never run that migration, create them once by hand first:

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.dashboard_setup.ensure_phase6_number_cards_and_dashboard
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.notification_setup.ensure_phase6_notifications
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase6_tests
```

Then check the public URL, not only the console — see above for why. The
checks that matter after a restart: `/api/method/ping` returns pong, `/login`
returns 200, `/app/research-study` and `/app/hospital-sign` resolve 200
(following the redirect —
an unauthenticated request 301s to `/login` first, which is expected),
`/app/query-report/Weekly Review` and `/app/dashboard-view/Hospital Ops`
resolve 200 the same way, and `https://sssihms.org` (the hospital's public
WordPress site, same VM) still returns 200.
