import os
import json
import time
import httpx

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DELAY = 0.05
MAX_RETRIES = 3


def get_access_token(creds_json):
    """Exchange refresh token for a fresh access token from credentials JSON."""
    creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _api(method, url, hdrs, timeout=15, **kwargs):
    """HTTP request with exponential backoff on 429."""
    time.sleep(DELAY)
    for attempt in range(MAX_RETRIES + 1):
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, url, headers=hdrs, **kwargs)
        if r.status_code == 429 and attempt < MAX_RETRIES:
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:500]
            raise Exception(f"HTTP {r.status_code}: {json.dumps(detail) if isinstance(detail, dict) else detail}")
        return r.json()


def do_list_spreadsheets(hdrs, query, limit):
    q = "mimeType='application/vnd.google-apps.spreadsheet'"
    if query:
        q += f" and name contains '{query}'"
    params = {"q": q, "pageSize": min(limit, 50), "fields": "files(id,name,modifiedTime,webViewLink)"}
    data = _api("GET", DRIVE_API, hdrs, params=params)
    files = data.get("files", [])
    return {
        "spreadsheets": [
            {"id": f["id"], "name": f["name"], "modified": f.get("modifiedTime", ""), "url": f.get("webViewLink", "")}
            for f in files
        ],
        "count": len(files),
    }


def do_get_sheet_info(hdrs, spreadsheet_id):
    data = _api("GET", f"{SHEETS_API}/{spreadsheet_id}", hdrs,
                params={"fields": "spreadsheetId,properties.title,sheets.properties"})
    sheets = data.get("sheets", [])
    return {
        "spreadsheet_id": data.get("spreadsheetId", ""),
        "title": data.get("properties", {}).get("title", ""),
        "sheets": [
            {
                "name": s["properties"]["title"],
                "index": s["properties"]["index"],
                "row_count": s["properties"].get("gridProperties", {}).get("rowCount", 0),
                "column_count": s["properties"].get("gridProperties", {}).get("columnCount", 0),
            }
            for s in sheets
        ],
    }


def _resolve_range(hdrs, spreadsheet_id, range_str):
    """Verify the sheet name in the range exists. If not, find closest match or fall back to first sheet."""
    if "!" in range_str:
        sheet_part, cell_part = range_str.split("!", 1)
    else:
        sheet_part, cell_part = range_str, ""

    requested = sheet_part.strip("'")

    try:
        info = _api("GET", f"{SHEETS_API}/{spreadsheet_id}", hdrs,
                     params={"fields": "sheets.properties.title"})
        sheets = info.get("sheets", [])
        if not sheets:
            return range_str

        sheet_names = [s["properties"]["title"] for s in sheets]

        # Exact match: sheet exists, return as-is
        if requested in sheet_names:
            return range_str

        # Case-insensitive match
        for name in sheet_names:
            if name.lower() == requested.lower():
                quoted = f"'{name}'" if " " in name else name
                return f"{quoted}!{cell_part}" if cell_part else quoted

        # Fuzzy match: check if any sheet name contains the requested name or vice versa
        req_lower = requested.lower()
        for name in sheet_names:
            if req_lower in name.lower() or name.lower() in req_lower:
                quoted = f"'{name}'" if " " in name else name
                return f"{quoted}!{cell_part}" if cell_part else quoted

        # No match: fall back to first sheet
        first = sheet_names[0]
        quoted = f"'{first}'" if " " in first else first
        return f"{quoted}!{cell_part}" if cell_part else quoted
    except Exception:
        return range_str


def do_read_sheet(hdrs, spreadsheet_id, range_str):
    range_str = _resolve_range(hdrs, spreadsheet_id, range_str)
    data = _api("GET", f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}", hdrs)
    values = data.get("values", [])
    return {"range": data.get("range", ""), "values": values, "rows": len(values)}


def do_write_cells(hdrs, spreadsheet_id, range_str, values):
    range_str = _resolve_range(hdrs, spreadsheet_id, range_str)
    if isinstance(values, str):
        values = json.loads(values)
    body = {"range": range_str, "majorDimension": "ROWS", "values": values}
    data = _api("PUT", f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}", hdrs,
                json=body, params={"valueInputOption": "USER_ENTERED"})
    return {"updated_range": data.get("updatedRange", ""), "updated_cells": data.get("updatedCells", 0)}


def do_append_rows(hdrs, spreadsheet_id, range_str, values):
    range_str = _resolve_range(hdrs, spreadsheet_id, range_str)
    if isinstance(values, str):
        values = json.loads(values)
    body = {"range": range_str, "majorDimension": "ROWS", "values": values}
    data = _api("POST", f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}:append", hdrs,
                json=body, params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"})
    updates = data.get("updates", {})
    return {"updated_range": updates.get("updatedRange", ""), "updated_rows": updates.get("updatedRows", 0)}


def do_create_spreadsheet(hdrs, title, sheet_names):
    body = {"properties": {"title": title}}
    if sheet_names:
        names = [n.strip() for n in sheet_names.split(",") if n.strip()]
        body["sheets"] = [{"properties": {"title": name}} for name in names]
    data = _api("POST", SHEETS_API, hdrs, json=body)
    return {
        "spreadsheet_id": data["spreadsheetId"],
        "title": data["properties"]["title"],
        "url": data.get("spreadsheetUrl", ""),
        "sheets": [s["properties"]["title"] for s in data.get("sheets", [])],
    }


try:
    token = get_access_token(os.environ["GOOGLE_SHEETS_CREDENTIALS_JSON"])
    hdrs = auth_headers(token)
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    action = inp.get("action", "")

    if action == "list_spreadsheets":
        result = do_list_spreadsheets(hdrs, inp.get("query", ""), inp.get("limit", 10))
    elif action == "get_sheet_info":
        result = do_get_sheet_info(hdrs, inp.get("spreadsheet_id", ""))
    elif action == "read_sheet":
        result = do_read_sheet(hdrs, inp.get("spreadsheet_id", ""), inp.get("range", "Sheet1"))
    elif action == "write_cells":
        result = do_write_cells(hdrs, inp.get("spreadsheet_id", ""), inp.get("range", "Sheet1"), inp.get("values", "[]"))
    elif action == "append_rows":
        result = do_append_rows(hdrs, inp.get("spreadsheet_id", ""), inp.get("range", "Sheet1"), inp.get("values", "[]"))
    elif action == "create_spreadsheet":
        result = do_create_spreadsheet(hdrs, inp.get("title", "Untitled"), inp.get("sheet_names", ""))
    else:
        result = {"error": f"Unknown action: {action}"}

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({"error": str(e)}))
