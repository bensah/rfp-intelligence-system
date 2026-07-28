"""Upload bytes to a Microsoft 365 OneDrive (Business) folder via Microsoft Graph.

Uses the APP-ONLY client-credentials flow — no interactive login, no refresh token to
expire — which is what an unattended cron needs. Dormant until the ONEDRIVE_* env is set,
so importing this never fails and nothing happens without configuration.

Env (set as GitHub Actions secrets for the backup workflow):
  ONEDRIVE_TENANT_ID      Azure AD tenant for the account (GUID, or the domain taadom.com)
  ONEDRIVE_CLIENT_ID      the app registration's Application (client) ID
  ONEDRIVE_CLIENT_SECRET  a client secret for that app registration
  ONEDRIVE_USER           drive owner UPN, e.g. nsah@taadom.com
  ONEDRIVE_FOLDER         destination folder under the drive root (default 'RFPIS Backups')

The app registration needs the APPLICATION permission `Files.ReadWrite.All` (Microsoft
Graph) with admin consent granted in the taadom.com tenant.
"""
from __future__ import annotations

import os

_GRAPH = "https://graph.microsoft.com/v1.0"
_AUTH = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Graph upload-session chunks must be multiples of 320 KiB. 3.2 MiB keeps requests small.
_CHUNK = 10 * 320 * 1024


def _cfg() -> dict:
    return {
        "tenant": os.getenv("ONEDRIVE_TENANT_ID"),
        "client_id": os.getenv("ONEDRIVE_CLIENT_ID"),
        "client_secret": os.getenv("ONEDRIVE_CLIENT_SECRET"),
        "user": os.getenv("ONEDRIVE_USER"),
        "folder": (os.getenv("ONEDRIVE_FOLDER") or "RFPIS Backups").strip("/"),
    }


def is_configured() -> bool:
    """True only when every required ONEDRIVE_* value is present."""
    c = _cfg()
    return all(c[k] for k in ("tenant", "client_id", "client_secret", "user"))


def folder_label() -> str:
    """Human label of the destination, for UI captions."""
    c = _cfg()
    return f"{c['user'] or '(unset)'} / {c['folder']}"


def _token(c: dict) -> str:
    import httpx
    r = httpx.post(
        _AUTH.format(tenant=c["tenant"]),
        data={"client_id": c["client_id"], "client_secret": c["client_secret"],
              "grant_type": "client_credentials",
              "scope": "https://graph.microsoft.com/.default"},
        timeout=30.0)
    r.raise_for_status()
    return r.json()["access_token"]


def upload_bytes(data: bytes, filename: str) -> dict:
    """Upload `data` to <ONEDRIVE_USER>'s drive under <ONEDRIVE_FOLDER>/<filename>,
    replacing any existing file of that name. Uses a chunked upload session so any size
    works. Returns the Graph driveItem JSON (name/id/webUrl). Raises on misconfig / HTTP
    error so the caller (cron / UI) surfaces it."""
    import httpx
    if not is_configured():
        raise RuntimeError("OneDrive backup is not configured (ONEDRIVE_* env not set).")
    c = _cfg()
    hdr = {"Authorization": f"Bearer {_token(c)}"}
    item_path = f"{c['folder']}/{filename}".strip("/")
    sess_url = (f"{_GRAPH}/users/{c['user']}/drive/root:/{item_path}:"
                "/createUploadSession")
    r = httpx.post(sess_url, headers=hdr,
                   json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                   timeout=30.0)
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]

    total = len(data)
    start = 0
    last_json: dict = {}
    with httpx.Client(timeout=120.0) as client:
        while start < total:
            end = min(start + _CHUNK, total) - 1
            piece = data[start:end + 1]
            put = client.put(
                upload_url, content=piece,
                headers={"Content-Range": f"bytes {start}-{end}/{total}"})
            put.raise_for_status()
            try:
                last_json = put.json() if put.content else {}
            except Exception:
                last_json = {}
            start = end + 1
    return last_json or {"name": filename}
