# User guide

The system is a website on the hotel network. Open the address IT gives you, for example `https://it-server.hotel.local`, and sign in with your username and password.
The first time you sign in, you must choose a new password of at least 10 characters.

What you see depends on your role. Every permission is checked by the server, so a hidden menu item cannot be reached by typing its address.

---

## Hotel employees

**Report a problem** (left menu, or scan the QR sticker on the device with your phone):

1. Choose the category, describe what happens, and say how much it affects your work and how urgent it is.
2. Attach photos, a short video or a screenshot if they help. Several files can be added at once.
3. Submit. You get a reference such as `INC-2026-00042`.

Under **Helpdesk Tickets** you can:
* follow your tickets and reply to questions from IT;
* add more evidence;
* when IT marks a ticket *Resolved*, **confirm it is fixed**, or say it is not and **reopen** it within the reopen period.

Notifications (the bell) tell you when IT replies or changes the status.

You cannot see other people's tickets or IT's internal notes.

## IT officers

* **Dashboard:** your day at a glance, including open and overdue tickets, today's tasks, maintenance due and charts.
* **Daily Planner** and **My Tasks:** plan the day and record progress.
  * Changing the date, putting work on hold or cancelling it needs a reason, which is kept in the history.
  * Unfinished work can be carried over to the next day.
* **Unplanned work** (top bar): record emergencies and walk-up requests as they happen, including which planned task they interrupted.
* **Timers:** start a timer on a task or ticket. Only one runs at a time; starting another stops the first.
  * You can also log time afterwards.
  * Time on a task linked to a ticket is counted against the ticket, once.
* **Helpdesk Tickets:**
  * assign, acknowledge and set status (*Waiting* needs the reason and who you are waiting on; this pauses the resolution target);
  * write internal notes or public replies;
  * attach before/after evidence;
  * link the asset or a project;
  * resolve with the resolution details.
  * Suggested knowledge-base articles appear on the ticket, with the reasons they matched.
* **My Activity Diary:** an automatic record of what you did. You can add entries yourself.
  * Entries are never deleted. A mistaken entry is *voided* with a reason.
* **Assets:**
  * the equipment register with QR labels and assignment and service history;
  * never store passwords or keys in it (the system refuses them).
* **Maintenance:** preventive plans, the calendar, checklists, repairs and compliance.
* **Projects**, **Procurement** (purchase and repair requests with approval) and the **Knowledge Base**.
* **Reports** and **Workload Analytics:** see below.

## Department heads / managers

* **Dashboard** and **Reports:** these show your department only; this is enforced by the server.
* **Procurement:** approve or reject purchase requests for your department. You cannot approve a request you raised yourself.
* **Projects:** follow the projects your department sponsors. When one is completed, read the benefits IT describes. If your department has seen them, click **Confirm benefits**. Only then do reports show them as *verified*. IT cannot confirm on your behalf, and if IT later edits the benefits, your confirmation is withdrawn until you confirm again.
* Report problems for yourself in the same way employees do.

## Reports (IT and managers)

**Reports** offers daily, weekly, monthly, annual and custom reports. You can view them on screen, print them, or download them as **PDF**, **Excel** or **CSV**.

Every figure carries a label:
* **Recorded:** counted from records in the system.
* **Reported by IT:** entered by IT staff, such as time logs and notes, and not independently checked.
* **Estimated:** an estimate entered by a person.
* **Verified:** confirmed by someone other than IT, for example the requester confirming a fix.

The system never estimates savings or productivity. Savings appear only when a project records them with evidence.

**Workload Analytics:**
* shows planned versus unplanned work, where time goes, resolution times by priority, backlog age, recurring incidents and maintenance compliance;
* includes plain-language notes on where workload is rising.

The per-person table is listed alphabetically and is **not a ranking**. Ticket counts alone do not measure performance.

## Administrators

**Administration** in the left menu:

| Page | Use |
|---|---|
| **Users** | Create accounts and set roles and departments. Set temporary passwords; users must change them at first sign-in. Deactivate leavers; accounts are never deleted, so history stays intact. |
| **Departments**, **Locations** | The hotel's structure, used for routing, reports and approvals. A department's head approves its purchase requests. |
| **Task / Ticket / Asset Categories**, **Service Targets** | Categories and the response/resolution targets per priority. Escalation rules are set on the service-targets page. |
| **System Configuration** | Hotel name and timezone, upload limits and file types, lockout policy, email notifications, ticket auto-close and reopen periods, procurement approval threshold, currency. Every change is recorded in the audit log. |
| **Audit Log** | Who did what, when and from where. It cannot be edited or deleted. Review *Access denied* and *Login locked out* events regularly. |
| **Backups** | Backup history and status, **Back up now** and **Verify**. Restoring is done on the server: see [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md). |

The dashboard turns the **Last successful backup** card red when no backup has succeeded for more than a day.
