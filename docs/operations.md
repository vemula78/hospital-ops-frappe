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

## Verification after any deploy

```bash
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 \
  bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase2_tests
```

32 invariant checks, all data rolled back regardless of outcome. Then check the
public URL, not only the console — see above for why.
