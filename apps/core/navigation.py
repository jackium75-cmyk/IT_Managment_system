"""
Sidebar definition. Each entry lists the roles that may see it. Entries whose
module is not built yet have ``url_name=None`` and are shown as disabled with
the phase in which they will be delivered, so no link ever leads nowhere.
"""
from django.urls import NoReverseMatch, reverse

from apps.accounts.roles import Role, get_role

ALL = tuple(Role.values)
IT = (Role.ADMIN, Role.IT_OFFICER)

SIDEBAR = [
    {"label": "Dashboard", "icon": "speedometer2", "url_name": "dashboard:index", "roles": IT + (Role.MANAGER,)},
    {"label": "My Home", "icon": "house", "url_name": "core:employee_home", "roles": (Role.EMPLOYEE,)},
    {"label": "Report a Problem", "icon": "plus-circle", "url_name": "helpdesk:create",
     "roles": (Role.EMPLOYEE, Role.MANAGER)},
    {"label": "My Tasks", "icon": "check2-square", "url_name": "tasks:list", "roles": IT},
    {"label": "Daily Planner", "icon": "calendar-week", "url_name": "tasks:planner", "roles": IT},
    {"label": "Recurring Tasks", "icon": "arrow-repeat", "url_name": "tasks:recurring_list", "roles": IT},
    {"label": "Helpdesk Tickets", "icon": "life-preserver", "url_name": "helpdesk:list", "roles": ALL},
    {"label": "My Activity Diary", "icon": "journal-text", "url_name": "diary:list", "roles": IT},
    {"label": "Assets", "icon": "pc-display", "url_name": "assets:list", "roles": IT},
    {"label": "Maintenance", "icon": "tools", "url_name": "maintenance:list", "roles": IT},
    {"label": "Projects", "icon": "kanban", "url_name": "projects:list", "roles": IT + (Role.MANAGER,)},
    {"label": "Procurement", "icon": "cart3", "url_name": "procurement:list", "roles": IT + (Role.MANAGER,)},
    {"label": "Knowledge Base", "icon": "book", "url_name": "knowledge:list", "roles": IT},
    {"label": "Reports", "icon": "file-earmark-bar-graph", "url_name": "reports:centre", "roles": IT + (Role.MANAGER,)},
    {"label": "Workload Analytics", "icon": "graph-up", "url_name": "reports:analytics", "roles": IT + (Role.MANAGER,)},
    {"label": "Notifications", "icon": "bell", "url_name": "notifications:list", "roles": ALL, "badge": "notifications"},
    {
        "label": "Administration", "icon": "gear", "roles": (Role.ADMIN,),
        "children": [
            {"label": "Users", "url_name": "accounts:user_list"},
            {"label": "Departments", "url_name": "accounts:department_list"},
            {"label": "Locations", "url_name": "accounts:location_list"},
            {"label": "Task Categories", "url_name": "tasks:category_list"},
            {"label": "Ticket Categories", "url_name": "helpdesk:category_list"},
            {"label": "Service Targets", "url_name": "helpdesk:sla_list"},
            {"label": "Asset Categories", "url_name": "assets:category_list"},
            {"label": "System Configuration", "url_name": "core:settings"},
            {"label": "Audit Log", "url_name": "core:audit_log"},
            {"label": "Backups", "url_name": "backups:list"},
        ],
    },
]


def _resolve(entry, path):
    item = dict(entry)
    url = None
    if item.get("url_name"):
        try:
            url = reverse(item["url_name"])
        except NoReverseMatch:
            url = None
    item["url"] = url
    item["active"] = False
    return item


def _matches(url, path):
    return bool(url) and (path == url or (url != "/" and path.startswith(url)))


def sidebar_for(user, path):
    role = get_role(user)
    items = []
    for entry in SIDEBAR:
        if role not in entry["roles"]:
            continue
        item = _resolve(entry, path)
        if "children" in entry:
            item["children"] = [_resolve(c, path) for c in entry["children"]]
        items.append(item)
    # Highlight only the most specific match (e.g. /tasks/planner/ over /tasks/).
    candidates = [i for i in items if _matches(i["url"], path)]
    candidates += [c for i in items for c in i.get("children", []) if _matches(c["url"], path)]
    if candidates:
        best = max(candidates, key=lambda i: len(i["url"]))
        best["active"] = True
        for item in items:
            if best in item.get("children", []):
                item["active"] = True
    return items
