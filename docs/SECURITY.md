# Security review (Phase 7)

This review covers the whole system as delivered. For each requirement in the specification, it says how the requirement is enforced and which automated tests prove it. It also lists what the Phase 7 review found and fixed, and the risks that remain for the hotel to manage.

Automated tests run against both SQLite and **PostgreSQL**, the production database (see the README, *Automated tests*).

## 1. Requirements and how they are enforced

| Requirement | How it is enforced | Tests |
|---|---|---|
| **Permissions on the backend, not just hidden buttons** | Every view is wrapped in `login_required` or `role_required`. Object-level checks live in the services, e.g. `can_view` and `require_it` in `helpdesk/services.py`. Denied attempts are written to the audit log. | `core/tests_security.py`: every URL is tried with GET and POST as an anonymous visitor (over 250 requests); employees are tried against every staff-only URL, and IT officers and managers against every administration URL. Per-module permission tests are in each app's tests. |
| **Never invent savings, productivity figures or results** | Savings are accepted only with written evidence (`Project.clean`) and listed only when documented. Reports compute everything from records, and show "—" where there is no data. | `projects/tests.py` (savings need evidence), `reports/tests.py` (only documented savings are listed). |
| **Reports distinguish completed, reported, estimated and verified work** | Every report figure carries a basis: *Recorded*, *Reported by IT*, *Estimated* or *Verified*. Each report includes a definitions section. *Verified* is used only for confirmation by someone other than IT: the requester confirming a fix, or the sponsoring manager confirming benefits from their own account. | `reports/tests.py`: requester-confirmed tickets are labelled *verified*; time is labelled *reported*. `projects/tests.py`: only the sponsoring manager can confirm benefits; IT cannot. |
| **Do not rank employees solely by tickets closed** | The workload table is alphabetical. It shows priority mix, time waiting on others and unplanned share next to counts, with a fairness note. Nothing in the system sorts staff by output. | `reports/tests.py` (the staff table is alphabetical). |
| **No secrets through QR codes or public asset pages** | A QR code holds only a random token, not the asset tag or any data. The scan page requires sign-in: IT staff see the asset, everyone else only gets "report a problem with this device". Asset records refuse text that looks like a password or key. There are no public pages at all. | `assets/tests.py` (scan requires sign-in, role-dependent result, credential guard); `core/tests_security.py` (sign-in required everywhere). |
| **Uploaded photos and videos not reachable by guessing their URL** | Files are stored under random names in `media_private`, which no URL serves. Every download goes through a view that checks the user may see the parent ticket, task or asset. Responses use `nosniff`, `private, no-store` and a sandboxing CSP. | `helpdesk/tests.py` (other users and anonymous visitors get 403/404; IT-only files; headers). |
| **Ordinary users cannot silently rewrite or delete history** | Records are never deleted: they are deactivated, voided with a reason, or retired. Status changes, reschedules, time voids and priority changes need a reason and are kept as history. The audit log is append-only: the model refuses updates and deletes. Since Phase 7, business records are **view-only** in Django's built-in admin site, and any change made there is copied into the audit log. | `core/tests.py` (audit immutability); `tasks/`, `helpdesk/`, `diary/` tests (reasons and history); `core/tests_security.py` (admin site view-only and audited). |
| **No double-counting when a ticket is linked to a task** | One rule, `diary/timecalc.py`. Time on a task linked to a ticket is stored once, on the ticket (`via_task`). Linking moves existing time instead of copying it. Every report and dashboard uses the same rule. | `helpdesk/tests.py`, `diary/tests.py`, `reports/tests.py` (time counted once). |
| **Sample data only through a clearly identified demo process** | `seed_demo` refuses to run unless `DJANGO_DEBUG=1` or `--allow-production` is given. Every demo record is labelled `demo_` or `[DEMO]`. `preflight` fails a production install that contains demo data. | `core/tests.py`; `preflight`. |

## 2. Authentication and sessions

* **Passwords:**
  * PBKDF2 hashing;
  * at least 10 characters;
  * common, numeric and user-similar passwords are rejected.
* **Temporary passwords:** they are issued by administrators and must be changed at first sign-in.
* **Lockout:**
  * a username is locked after 5 failures (configurable);
  * an IP address is locked after 20, which stops password spraying;
  * the lockout is stored in the database, so it survives restarts.
  * Behind a reverse proxy, Waitress trusts `X-Forwarded-For` only from `127.0.0.1` (`-BehindProxy`), so a client cannot spoof its IP.
* **Session cookies:** HttpOnly and SameSite=Lax. They end when the browser closes and last at most 8 hours. With `DJANGO_USE_HTTPS=1` they are also Secure, and HTTPS redirect and optional HSTS are on.
* **Sign-out** is POST-only, so a link cannot sign someone out.
* **CSRF** protection is on every form.
* **Session housekeeping:** expired sessions are deleted by the scheduled jobs. Lockout counters are deleted after 90 days. Failed sign-ins remain permanently in the audit log.

## 3. Browser-side protections (added in Phase 7)

* **Content-Security-Policy** on every page: `script-src 'self'`, so no inline or third-party scripts can run.
  * Also `object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'` and `base-uri 'self'`.
  * All JavaScript was moved out of templates, and a test fails if an inline script or `on…=` handler is added again.
  * Even if some text escaped Django's auto-escaping, it could not run script.
* **Permissions-Policy:** microphone, geolocation, payment and USB are disabled. The camera is allowed for this site only, for photo evidence.
* **`Cache-Control: private, no-store`** on every page for signed-in users. On a shared front-desk PC, pressing *Back* after signing out does not show the previous user's tickets.
* **Other headers:** `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`.
* **No third-party resources:** Bootstrap, the icons and Chart.js are served locally (also enforced by a test). The system works without internet access and leaks nothing to CDNs.

## 4. Uploads

* An allow-list is set by administrators. Executable and script types can never be enabled.
* Content must match the extension:
  * magic bytes are checked;
  * images are verified by Pillow, with a decompression-bomb limit;
  * Office files must be real OOXML without macros;
  * text files must not contain binary data.
* Size and count limits apply. SHA-256 fingerprints are stored. Original bytes are kept as evidence.

## 5. Data protection and backups

* Secrets (secret key, database and email passwords) live only in `.env`. The installer restricts that file to Administrators, SYSTEM and the service account.
* The uploads, backups and logs folders are restricted in the same way.
* Backups:
  * contain fingerprints of every file and are verified by a test restore;
  * are never downloadable through the web;
  * failures alert administrators.
  * See [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).
* Exports (CSV and Excel) neutralise spreadsheet formula injection. Report downloads are `private, no-store`.
* Generating a report, exporting, and every backup, verification and restore are recorded in the audit log.

## 6. Findings fixed during this review

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | On PostgreSQL, several workflow actions failed with *"FOR UPDATE cannot be applied to the nullable side of an outer join"*: completing a maintenance visit, starting a task timer, deciding a purchase request. SQLite, used in development, ignores row locks, so earlier tests missed it. | High (production outage of those actions) | Row locks now lock only the record itself (`select_for_update(of=("self",))`). The full test suite now also runs on PostgreSQL. |
| 2 | Superusers could edit tickets, tasks, projects, purchase requests and other records in Django's admin site. That bypassed the business rules and the application's audit log. | Medium | Business records are view-only there. Any remaining change, such as configuration or accounts, is copied into the audit log with the administrator's name. |
| 3 | No Content-Security-Policy. Several templates used inline `onclick`/`onchange` handlers and one inline script. | Medium (defence in depth) | Strict CSP added; inline code moved to `static/js/app.js`; regression test. |
| 4 | Signed-in pages could be cached by the browser and redisplayed with *Back* after sign-out on shared PCs. | Low–medium | `Cache-Control: private, no-store` for signed-in users. |
| 5 | Running behind an HTTPS reverse proxy was not supported. Waitress listened on every interface and did not process forwarding headers, so the audit log and the per-IP lockout would have recorded the proxy's address for every user. Clients could also bypass the proxy and reach Waitress directly. | Medium (deployment) | `-BehindProxy` binds Waitress to `127.0.0.1` and trusts `X-Forwarded-Proto` and `X-Forwarded-For` only from the local proxy (options checked against Waitress 3). |
| 6 | Expired sessions and old lockout counters were never removed. | Low | Housekeeping in the scheduled jobs. |
| 7 | Project benefits could be shown as *verified* because an IT officer picked a manager's name on the project form. That is not independent verification. | Medium (report integrity) | Only a manager of the sponsoring department can confirm, personally, on the project page, with a timestamp and an audit entry. Editing the benefits or savings afterwards withdraws the confirmation. |

## 7. Remaining risks and recommendations

These are outside the application, or are deliberate scope limits. The hotel should manage them:

1. **HTTPS is optional** so that the system can run on day one. Without it, passwords cross the LAN in clear text. Set up HTTPS before going live ([DEPLOYMENT.md §8](DEPLOYMENT.md#8-https)); `preflight` warns until it is on.
2. **No multi-factor authentication.**
   * Mitigations: lockout, LAN-only access, VPN for remote use.
   * If the system must ever be reachable from the internet, put it behind the hotel's VPN or an identity-aware proxy with MFA. Never use a plain port-forward.
3. **Backups are not encrypted by the application.** Store them on BitLocker-encrypted disks and restricted shares.
4. **Superusers** can still change configuration and accounts in Django's admin site (audited) and can read everything. Keep only one or two superuser accounts, for the IT manager, and use normal *System Administrator* accounts day to day.
5. **Email** notifications contain ticket subjects. Use a mail server that supports TLS (`EMAIL_USE_TLS=1`).
6. **Patching:**
   * Apply Windows, Python and PostgreSQL security updates monthly.
   * Update the Python packages (`requirements.txt` pins compatible ranges) with `scripts\update.ps1`, which backs up first.
7. **Account hygiene:**
   * Deactivate leavers on their last day.
   * Review the user list and the audit log's *Access denied* and *Login locked out* events monthly.
