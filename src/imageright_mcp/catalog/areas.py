"""Functional areas: one browsable grouping across REST v1, REST v2 and SOAP.

The generated catalog keeps each surface's own grouping (OAS tags for REST, the report's
area tables for SOAP). Those don't line up, so explorer tools group by the areas below.
All descriptions are our own wording.
"""

from __future__ import annotations

import re
from typing import Any

AREAS: dict[str, str] = {
    "Session": "Logging in, token lifetime, licensing seats and server health checks.",
    "Users & security": "User and group accounts, roles, functionality rights and permissions.",
    "Drawers": "Top-level cabinets that hold files, and the locations they sit in.",
    "Files": "Files (the main record, e.g. a policy or claim): create, find, read, update, link.",
    "Folders": "Folders inside files that organise documents.",
    "Documents": "Documents inside files or folders; each holds an ordered set of pages.",
    "Containers": "Generic reads over anything that holds content: children, paths, parents.",
    "Pages": "Pages and their images: add, replace, move, lock, version, stream.",
    "Batches": "Capture batches that group newly added pages for tracking.",
    "Marks": "File and page marks (flags) and their definitions.",
    "Notes": "Notes attached to objects.",
    "Attributes": "Custom attributes (index fields) on files, folders, documents and pages.",
    "Types": "Object types and templates: which document, folder and file types exist.",
    "Workflow": "Workflows, their steps, links between steps, and who may work each step.",
    "Tasks": "Workflow tasks: create, find, lock, route, release, history, dashboards, SLAs.",
    "OCR & validation": "OCR form setup, extracted data sets and data-validation fields.",
    "Redaction": "Redaction rule sets.",
    "Reports": "Report templates, parameters and generated report output.",
    "Configuration": "Global and per-user configuration values and device settings.",
    "Integration": "Hooks for agency-management systems, portals and client-side events.",
    "Email receiver": "Mailboxes that turn incoming email into content (25.1 and later).",
}

_BY_NATIVE: dict[str, str] = {
    "Authentication": "Session",
    "Licensing": "Session",
    "Health": "Session",
    "Instrumentation": "Session",
    "ServiceDiscovery": "Session",
    "Accounts": "Users & security",
    "Users": "Users & security",
    "FunctionalityRights": "Users & security",
    "Permissions": "Users & security",
    "Encryption": "Users & security",
    "Drawers": "Drawers",
    "Files": "Files",
    "Folders": "Folders",
    "Documents": "Documents",
    "DocumentFilter": "Documents",
    "Containers": "Containers",
    "Instances": "Containers",
    "Pages": "Pages",
    "PageVersions": "Pages",
    "Images": "Pages",
    "Overlays": "Pages",
    "PhoneCalls": "Pages",
    "Batches": "Batches",
    "Marks": "Marks",
    "Notes": "Notes",
    "Attributes": "Attributes",
    "ObjectTypes": "Types",
    "Types / templates / attribute metadata": "Types",
    "Workflow": "Workflow",
    "Tasks": "Tasks",
    "TaskActions": "Tasks",
    "TaskAttributes": "Tasks",
    "TaskHistory": "Tasks",
    "Dashboard": "Tasks",
    "DashboardViews": "Tasks",
    "Sla": "Tasks",
    "OcrForms": "OCR & validation",
    "OcrFormVersions": "OCR & validation",
    "OcrFolders": "OCR & validation",
    "OcrFields": "OCR & validation",
    "OcrDataSets": "OCR & validation",
    "OcrDataForms": "OCR & validation",
    "DataValidation": "OCR & validation",
    "RedactionRuleSets": "Redaction",
    "Reports": "Reports",
    "ReportTemplates": "Reports",
    "ReportParameters": "Reports",
    "GlobalConfig": "Configuration",
    "UserConfig": "Configuration",
    "Misc": "Configuration",
    "Integration": "Integration",
    "Collaboration Portal": "Integration",
    "ClientActions": "Integration",
    "EmailReceiver": "Email receiver",
}

# SOAP groups that span several areas: first matching name pattern wins.
_SOAP_SPLITS: dict[str, list[tuple[str, str]]] = {
    "Files / folders / documents / containers": [
        (r"Drawer|Location", "Drawers"),
        (r"Document", "Documents"),
        (r"Folder", "Folders"),
        (r"File", "Files"),
        (r".*", "Containers"),
    ],
    "Pages / images": [(r"Batch", "Batches"), (r".*", "Pages")],
    "Workflow / tasks": [(r"Task", "Tasks"), (r".*", "Workflow")],
    "Session / identity": [
        (r"^(UserLogin|UserLogoff|IsLoggedIn|AvailableConnections|Version)$", "Session"),
        (r".*", "Users & security"),
    ],
}


def area_of(op: dict[str, Any]) -> str:
    native = str(op["area"])
    splits = _SOAP_SPLITS.get(native)
    if splits is not None:
        name = str(op.get("operation") or op["id"])
        for pattern, area in splits:
            if re.search(pattern, name):
                return area
    return _BY_NATIVE.get(native, "Configuration")


def match_area(text: str) -> str | None:
    """Case-insensitive match on an area name, also accepting a native OAS tag."""
    wanted = text.strip().lower()
    for area in AREAS:
        if area.lower() == wanted:
            return area
    for native, area in _BY_NATIVE.items():
        if native.lower() == wanted:
            return area
    return None
