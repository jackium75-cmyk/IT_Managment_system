# Hotel IT Management System

This is a web-based system for the hotel's IT work. It covers the helpdesk, daily task planning, assets, maintenance, projects, procurement, reporting and backups.
It is built with Django 5.2 LTS, PostgreSQL (SQLite for development) and Bootstrap 5. It runs on Windows and serves users across the hotel's local network. It needs no internet access.

| Document | For |
|---|---|
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Staff: what each role can do and how |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | IT: production installation on Windows, HTTPS, updates, troubleshooting |
| [`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md) | IT: backups, verification, restore and disaster recovery |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Management/IT: security review, how each rule is enforced, remaining risks |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Developers: architecture, modules, data model |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Everyone: completed features, and what is deliberately not included |

**Current release: Phase 7 (complete system).**

* **Phase 1:** sign-in, roles and permissions, users, departments and locations, system configuration, the audit log and the dashboard.
* **Phase 2:** tasks, the daily and weekly planner, recurring tasks, time tracking, unplanned work and interruptions, and the activity diary.
* **Phase 3:** the helpdesk (employee portal, tickets, lifecycle, troubleshooting history), secure photo/video/document evidence, notifications, response/resolution targets and escalation.
* **Phase 4:** the IT asset register (QR codes, service history, assignment history, documents, Excel/CSV export) and preventive/corrective maintenance (plans, checklists, calendar, reminders, compliance, repeated-failure analysis).
* **Phase 5:** IT projects (milestones, progress log, linked work, time and cost roll-ups, completion report), purchase/repair requests with approvals, a troubleshooting knowledge base with solution suggestions on tickets, and global search.
* **Phase 6:** the reports centre (daily, weekly, monthly, annual and custom reports as PDF, Excel and CSV) and workload analytics.
* **Phase 7:**
  * security review and hardening (Content-Security-Policy, admin-site lock-down, PostgreSQL locking fix);
  * verified backups with restore and rotation;
  * Windows installation, scheduled-task and update scripts;
  * a production readiness check;
  * the full documentation set.

---

## 1. Quick trial on one PC (SQLite)

Install **Python 3.12 or 3.13** from <https://www.python.org/downloads/windows/> and tick **"Add python.exe to PATH"**. Then, in **PowerShell**, in the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env          # set DJANGO_DEBUG=1 for a trial
python manage.py migrate
python manage.py seed_demo     # optional demo data (see section 7)
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Press **Ctrl+C** to stop.

> If PowerShell refuses to run `Activate.ps1`, run this once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 2. Production installation (summary)

Follow [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). In short, in an **Administrator PowerShell**:

1. Install **PostgreSQL 16/17** and create the `hotel_it` database and user (DEPLOYMENT.md §2).
2. Run `scripts\install.ps1` to set up the virtual environment, packages, `.env`, the data folders and their permissions, migrations and static files. Fill in `.env` and run it again.
3. Run `.\.venv\Scripts\python.exe manage.py createsuperuser` to create the first administrator.
4. Run `scripts\register_tasks.ps1` to register the scheduled tasks:
   * **HotelIT Server**: Waitress at boot, restarted automatically;
   * **HotelIT Jobs**: every 10 minutes;
   * **HotelIT Backup**: nightly, verified.
5. Set up HTTPS (Caddy or IIS in front of Waitress; DEPLOYMENT.md §8) and open the firewall port.
6. Run `.\.venv\Scripts\python.exe manage.py preflight` until it reports no **FAIL**.

To update to a new version later, run `scripts\update.ps1`. It backs up first.

## 3. Database migrations

`scripts\install.ps1` and `scripts\update.ps1` apply migrations for you. By hand:

```powershell
python manage.py migrate
```

`showmigrations` lists which migrations have been applied.

## 4. The first administrator

```powershell
python manage.py createsuperuser
```

Superusers are always **System Administrators** in the application. Sign in and go to **Administration**. From there, create departments, locations and user accounts with these roles:

* **IT Support Officer**
* **Department Head / Manager**
* **Hotel Employee**

New users receive a temporary password. They must change it the first time they sign in.

Keep superuser accounts to one or two. Django's built-in admin site (`DJANGO_ADMIN_URL`) is for emergencies only: business records are view-only there, and every change made there is copied into the audit log.

## 5. Running the system

In production the **HotelIT Server** scheduled task runs Waitress. Use `Stop-ScheduledTask "HotelIT Server"` and `Start-ScheduledTask "HotelIT Server"` to control it.

To see errors while troubleshooting, run it in a window instead:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1 -Port 8000
```

Firewall: allow the port for the **private/domain** profiles only. Never expose the system directly to the internet.

## 6. Scheduled jobs, backups, email and evidence

### Scheduled jobs (escalations, maintenance, reminders)

`run_jobs` does all of the following:

* generates recurring tasks (`generate_recurring_tasks` is still available on its own);
* creates maintenance tasks and reminders, and marks missed maintenance visits;
* runs the ticket escalation rules;
* closes resolved tickets the requester never confirmed;
* sends overdue-task reminders;
* removes expired sign-in sessions and lockout counters older than 90 days. The audit log is never pruned.

It also runs automatically (at most every 5 minutes) when IT staff open the dashboard, planner, helpdesk or maintenance pages. The **HotelIT Jobs** task runs it every 10 minutes so this still happens at night:

```powershell
python manage.py run_jobs
```

If you already scheduled `run_helpdesk_jobs` (Phase 3), it keeps working: it is now an alias of `run_jobs`.

### Backups

The **HotelIT Backup** task runs `manage.py backup --scheduled --verify` nightly. Each backup:

* contains the database and every uploaded file, with SHA-256 fingerprints;
* is test-restored to verify it;
* is copied to `BACKUP_COPY_TO`;
* is removed by rotation once old.

Administrators see the history under **Administration → Backups**. To restore, use `manage.py restore_backup` on the server. Full procedures are in [`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md).

### Email notifications

In-app notifications always work. To send email as well:

1. Set the `EMAIL_*` values and `SITE_BASE_URL` in `.env`.
2. Restart the server.
3. Tick **Email notifications** under *Administration → System Configuration*.

Each user can switch email off in *My profile*. SMS and WhatsApp are not included; they need a paid, authorised provider.

### Evidence files

Uploads are stored in `media_private\evidence\YYYY\MM\` under random names. They are never served directly: every download checks the user's permission. Nightly backups include them.

## 7. Demo data (development only)

```powershell
$env:DJANGO_DEBUG="1"; python manage.py seed_demo
```

This creates clearly-labelled demo departments, locations and accounts. All demo usernames start with `demo_`, for example `demo_admin`, `demo_it`, `demo_fo_manager` and `demo_receptionist`. The demo password is `Demo-Hotel-2026!`.
It also creates demo tasks, recurring checks, time entries, an interruption and a diary entry for `demo_it`. Their titles start with `[DEMO]`.
It also creates demo tickets in different states, with photo evidence, a task linked to a ticket, and notifications. Their subjects start with `[DEMO]`.
It also creates a demo Wi-Fi upgrade project, purchase requests in different states (ordered, awaiting approval by the F&B manager, delivered) and three knowledge articles.
Finally it creates 7 demo assets with maintenance plans in different states (done, due soon, overdue) and a recorded repair. Demo asset notes start with `[DEMO]`.
The command refuses to run when `DJANGO_DEBUG` is off. **Do not load demo data into the live database.**

## 8. Automated tests

```powershell
$env:DJANGO_DEBUG="1"; python manage.py test
```

Production uses PostgreSQL, so run the tests against it too before each release. SQLite ignores row locks and would hide some faults. The database user needs the `CREATEDB` right, because the tests create and remove a temporary `test_hotel_it` database:

```powershell
$env:DJANGO_DEBUG="1"; $env:DB_ENGINE="postgres"; python manage.py test
```

The tests cover:

* Authentication, lockout and forced password change.
* Role permissions for every administration URL.
* User, department and location administration.
* Audit log immutability and filtering.
* System configuration validation.
* Dashboard figures against database records, and the manager's restriction to their own department.
* Task rules:
  * numbering and permissions;
  * reasons required for delays, cancelling and reopening;
  * completion, rescheduling and carry-over;
  * timers, overlap rejection and voiding time;
  * interruptions;
  * recurrence rules and idempotent generation.
* The activity diary and the single time-counting rule.
* Upload validation, including disguised, executable, macro and oversized files.
* Who can see each ticket, internal note and file. Files cannot be reached by guessing their address, and they are served with safe headers and video seeking.
* The full ticket lifecycle:
  * waiting pauses the resolution target;
  * requester confirmation, reopening and the reopen window;
  * auto-close;
  * priority changes.
* Escalation rules fire once, and duplicate notifications are suppressed. Email is sent only after the action is saved, and respects each user's opt-out.
* Time on a task linked to a ticket is counted against the ticket, once.
* Assets:
  * tagging, validation and the credential guard;
  * duplicate serial numbers;
  * assignment history;
  * retiring an asset with a reason stops its maintenance;
  * IT-only access, and CSV/Excel export with formula-injection protection.
* QR scanning: sign-in is required and the result depends on the role. The employee "report this device" flow is covered.
* Maintenance:
  * tasks created within the lead time, with no duplicates;
  * checklist completion and next-visit scheduling;
  * missed visits after the grace period;
  * rescheduling counted honestly in compliance;
  * repairs from tickets;
  * repeated-failure counts and attention rules.
* Projects:
  * the completion rule;
  * savings must have evidence;
  * milestone progress;
  * time and costs counted once;
  * department visibility;
  * report wording.
* Procurement:
  * approval only by the department head or an administrator, never the requester;
  * rejection and resubmission;
  * amounts locked after submission;
  * automatic approval below the threshold;
  * order and delivery, with costs shown in the asset's history.
* Knowledge base:
  * matching on error code and device type, with reasons;
  * weak matches and drafts are not suggested;
  * writing an article from a ticket, and applying one to a ticket;
  * the credential guard;
  * verification.
* Global search respects each role's permissions.
* Reports and analytics:
  * every report kind builds, renders and exports as PDF, Excel and CSV;
  * figures match the records, and time is counted once;
  * every figure carries its basis label;
  * managers are confined to their department, and employees are refused;
  * exports neutralise formula injection;
  * staff are listed alphabetically, not ranked.
* Security sweep (Phase 7):
  * every URL refuses anonymous visitors (GET and POST);
  * employees cannot reach any staff page, and IT officers and managers cannot reach administration;
  * the security headers (CSP, no-store, Permissions-Policy) are sent;
  * templates contain no inline scripts or third-party resources;
  * Django's admin site is view-only for business records and audited;
  * housekeeping never touches the audit log.
* Project benefits are *verified* only when the sponsoring manager confirms them personally.
* Backups:
  * the archive contents and fingerprints;
  * damaged, altered and malicious (path-traversal) archives are rejected;
  * a restore round trip, a safety backup before restoring, and refusal of backups from newer versions;
  * one backup at a time;
  * failures are recorded and administrators alerted;
  * rotation;
  * the admin-only backup page.

## 9. Project layout

```
config/            settings, URLs, WSGI
apps/core/         system settings, audit log, permissions, navigation, middleware
apps/accounts/     roles, user profiles, departments, locations, login throttling
apps/dashboard/    dashboard view and metric registry (later modules plug in here)
apps/tasks/        tasks, planner, recurring schedules, time entries, business rules (services.py)
apps/diary/        activity diary, the time-counting rule (timecalc.py) and timers (timekeeping.py)
apps/evidence/     upload validation, private storage, safe file serving
apps/helpdesk/     tickets, categories, service targets, escalation, business rules (services.py)
apps/notifications/ notification centre and email
apps/assets/       asset register, QR codes, service history, attention rules (services.py)
apps/maintenance/  maintenance plans, visits, repairs, compliance, scheduled job (services.py)
apps/core/jobs.py  all scheduled jobs in one place (run by `manage.py run_jobs`)
apps/projects/     projects, milestones, progress log, time/cost roll-ups, report
apps/procurement/  purchase/repair requests, approvals, expenditure (services.py has the workflow)
apps/knowledge/    troubleshooting articles; matching.py explains how suggestions are scored
apps/reports/      reports centre: context (filters), sections, builders, exporters (PDF/Excel/CSV), analytics
apps/backups/      backup engine (create/verify/restore), records, rotation, backup page and commands
templates/         Bootstrap 5 templates
static/            CSS/JS + vendored Bootstrap, Bootstrap Icons, Chart.js (works offline)
media_private/     uploaded evidence (never served directly)
logs/              rotating application log
scripts/           Windows scripts: install, register_tasks, start_server, update
docs/              user guide, deployment, backup and restore, security review, architecture, roadmap
backups/           default BACKUP_ROOT (set it to another disk in production)
```

## 10. Defaults and assumptions

* **Times** are stored in UTC and shown in the timezone set under *Administration → System Configuration*.
* **Sessions** expire when the browser closes, or after 8 hours (one shift).
* **Sign-in lockout:** 5 failures lock the account for 15 minutes. Both values are configurable.
* **Records are not deleted.** Departments, locations and users are *deactivated*, so their history and reports remain intact.
* **Managers** see only their own department on the dashboard.
* **Employees** land on a simple home page. The helpdesk portal arrives in Phase 3.
* **Tasks** are for IT staff only:
  * IT officers can view all tasks;
  * IT officers can change tasks assigned to them or created by them;
  * administrators can change any task.
* **Diaries:** officers see only their own; administrators can open anyone's.
* **Time is counted once.** Time comes from task time entries, ticket time entries and manual diary entries, and overlapping entries for the same person are refused.
  * **Linked tasks:** when a task is linked to a ticket, its time is stored on the ticket. Linking moves time already recorded on the task.
  * **Planned or unplanned:** ticket time counts as unplanned (reactive) work unless it was done through a planned task.
* **One timer per person**, across tasks and tickets.
* **A task's date or time** is changed only through *Reschedule*, which requires a reason. The originally planned date is always kept.
* **Waiting, blocked, cancelled or reopened** tasks need a reason. *Completed* needs completion notes.
* **A task is late** if it is completed after its (latest) planned date. It is overdue if it is still open after that date.
* **Unplanned work** is recorded as its own task, flagged as unplanned. The interrupted task is not changed, apart from a timeline note.
* **Timers** round to the nearest minute (minimum 1). Starting a timer on one task stops any timer already running.
* **Mistaken time and manual diary entries are voided** with a reason, never deleted. System diary entries cannot be edited.
* **Django's built-in admin** stays available at `/django-admin/` (configurable via `DJANGO_ADMIN_URL`) for emergency superuser maintenance. Day-to-day administration uses the application's own pages, which are audited.

### Helpdesk rules

* **Who sees what:**
  * employees see their own tickets;
  * managers see their own tickets plus their department's;
  * IT staff see every ticket.
  * Internal notes, troubleshooting steps and files marked *IT only* are never shown to requesters.
* **Priority** is calculated from the requester's answers: *who is affected* × *how much it stops work*. IT can override it with a reason, which recalculates the targets.
* **Targets** are measured in calendar time, because the hotel runs 24/7. Time spent *waiting* (on the requester, a vendor, parts or an approval) pauses the resolution target. Resolution-time statistics also exclude waiting time.
* **When the requester replies** while a ticket waits on them, the ticket goes back to *In progress* automatically.
* **Confirmation and closing:**
  * resolved tickets ask the requester to confirm the fix;
  * unconfirmed tickets close automatically after 3 days;
  * closed tickets can be reopened by the requester for 14 days, and by IT at any time;
  * reopening keeps the full history.
  * Both periods are configurable.
* **Attachments are never deleted.** IT can hide one with a reason, which is audited.

### Asset and maintenance rules

* **Asset IDs** are generated from the category code (`PRN-0001`, `PC-0001`, …) and never change. The category of an existing asset therefore cannot be changed.
* **QR codes** contain only a random token and a link, never the asset's details.
  * Scanning requires sign-in.
  * IT staff see the asset record.
  * Everyone else gets "report a problem with this device", pre-filled, without seeing any asset details.
* **No credentials in the register.** Text that looks like a password, Wi-Fi key or licence key (for example `password: …`) is refused, because the register can be printed and exported.
* **Assets are never deleted.** They are retired, disposed of or reported lost, with a reason; this also stops their maintenance plans. Moves and reassignments are kept in the assignment history.
* **"Needs attention"** means one or more of:
  * under repair;
  * poor or broken condition;
  * replacement recommended;
  * warranty ending within 60 days;
  * older than the category's expected life;
  * 3 or more failures (incident tickets plus repairs) in 90 days;
  * overdue maintenance.
* **Maintenance plans:**
  * Each plan has one open visit at a time.
  * A planner task is created `lead_days` before the visit is due (7 by default), and the time is recorded on that task.
  * Completing a visit schedules the next one from the completion date.
  * A visit not done within the grace period (7 days, configurable) is marked **missed**, and the next one keeps the original rhythm.
* **Compliance** counts each visit in the period of its *original* due date.
  * *On time* means done by that original date, so rescheduling does not hide lateness.
  * Compliance = on time ÷ (completed + missed).
* **Costs** are shown per currency and never converted or mixed. Only costs that were actually recorded are reported.

### Project, procurement and knowledge-base rules

* **Projects:**
  * Everyone in IT can see and work on projects. Managers can view, read-only, the projects sponsored by their department.
  * A project can only be marked *Completed* once its final outcome is described.
  * **Documented savings** are only accepted with an explanation of the evidence. Benefits are labelled "reported by IT" until a manager of the sponsoring department confirms them personally on the project page (IT cannot do it for them; editing the benefits withdraws the confirmation). Reports never invent savings or productivity figures.
  * **Project time** is the once-only time recorded on the project's tasks and tickets.
  * **Project costs** are kept per currency:
    * estimated cost;
    * approved purchase requests;
    * actual spend (delivered purchases plus other recorded costs).
* **Procurement:**
  * IT staff raise requests, which go through draft → awaiting approval → approved → ordered → delivered. A request can also be rejected (then edited and resubmitted) or cancelled.
  * Approvers are administrators and the head of the department the request is for. **Nobody can approve their own request.**
  * Requests below the configured threshold are approved automatically. The threshold is set under *System Configuration* and is 0 by default, meaning everything needs approval. Approval can also be switched off.
  * The item, amount and reason are locked once submitted.
  * Delivered amounts appear in the asset's service history and the project's costs.
  * This is an expenditure log for IT, not an accounting system.
* **Knowledge base (IT only):**
  * Articles hold the full troubleshooting record and are marked *verified* or *unverified*. Text that looks like a password or key is refused.
  * Published articles are suggested on tickets, scored by: shared error codes (strongest), same device type, same category, shared keywords, and verified status.
  * Each suggestion shows why it matched, with a reminder that similar symptoms can have different causes.
  * *I'm using this solution* records the link on the ticket as an internal note.
* **Search** (top bar) looks across tickets, tasks, assets, projects, purchase requests, articles and your own diary. It only searches records your role may see.

### Reports and analytics rules

* **Who can run reports:** administrators and IT staff see everything. Managers see only their own department; the department filter is fixed on the server. Employees cannot open reports.
* **Labels on figures:**
  * *Recorded* – counted from records in the system;
  * *Reported by IT* – entered by IT staff (time logs, notes, benefits) and not independently checked;
  * *Estimated* – an estimate entered by a person;
  * *Verified* – confirmed by someone other than IT (the requester confirmed the fix, or a manager confirmed project benefits).
* **Time** is counted once: time on a task linked to a ticket counts against the ticket.
* **Resolution time** excludes time a ticket spent waiting on the requester, vendors, parts or approval.
* **Savings** appear only when they were documented with evidence. The system never estimates savings or productivity.
* **Staff workload** is listed alphabetically with context (priority mix, waiting time, unplanned share). Nobody is ranked by tickets closed.
* **Exports:** PDF, Excel and CSV are generated on demand and not stored on the server. Values that could run as spreadsheet formulas are neutralised. Every report generation is recorded in the audit log.
* **Period limits:** daily = 1 day, weekly ≤ 31 days, monthly ≤ 93 days, annual and custom ≤ 400 days. Longer requests are shortened.
