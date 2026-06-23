---
name: google-sheets
display_name: "Google Sheets"
description: "Read, write, and manage Google Sheets spreadsheets"
category: productivity
icon: table
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25,google-auth>=2.0,requests>=2.20"
resource_requirements:
  - env_var: GOOGLE_SHEETS_CREDENTIALS_JSON
    name: "Google Sheets Credentials"
    description: "Google OAuth credentials JSON (auto-provided by gateway connection)"
  - env_var: GOOGLE_SHEETS_USER_EMAIL
    name: "Google Account Email"
    description: "Email of the connected Google account"
tool_schema:
  name: google_sheets
  description: "Read, write, and manage Google Sheets spreadsheets"
  parameters:
    type: object
    properties:
      action:
        type: "string"
        description: "Which operation to perform"
        enum: ['list_spreadsheets', 'read_sheet', 'write_cells', 'append_rows', 'create_spreadsheet', 'get_sheet_info']
      spreadsheet_id:
        type: "string"
        description: "Spreadsheet ID (from URL) — for read_sheet, write_cells, append_rows, get_sheet_info"
        default: ""
      range:
        type: "string"
        description: "A1 notation range — for read_sheet, write_cells, append_rows (e.g. 'Sheet1!A1:D10', 'Sheet1')"
        default: "Sheet1"
      values:
        type: "string"
        description: "JSON array of arrays (rows) — for write_cells, append_rows (e.g. [[\"Name\",\"Email\"],[\"John\",\"john@co.com\"]])"
        default: ""
      title:
        type: "string"
        description: "Spreadsheet title — for create_spreadsheet"
        default: ""
      sheet_names:
        type: "string"
        description: "Comma-separated sheet names — for create_spreadsheet (e.g. 'Leads,Pipeline,Stats')"
        default: ""
      query:
        type: "string"
        description: "Search query — for list_spreadsheets (searches by name)"
        default: ""
      limit:
        type: "integer"
        description: "Max results — for list_spreadsheets (default 10)"
        default: 10
    required: [action]
---
# Google Sheets

Read, write, and manage Google Sheets spreadsheets.

## Navigation
- **list_spreadsheets** — List spreadsheets in Google Drive. Optionally filter by `query` (name search).
- **get_sheet_info** — Get spreadsheet metadata (sheet names, row counts). Provide `spreadsheet_id`.

## Reading
- **read_sheet** — Read cell values from a range. Provide `spreadsheet_id` and `range` (A1 notation like `Sheet1!A1:D10`).

## Writing
- **write_cells** — Write values to a range (overwrites existing). Provide `spreadsheet_id`, `range`, and `values` (JSON array of row arrays).
- **append_rows** — Append rows after existing data. Provide `spreadsheet_id`, `range` (target sheet), and `values` (JSON array of row arrays).

## Creation
- **create_spreadsheet** — Create a new spreadsheet. Provide `title` and optional `sheet_names` (comma-separated).

## Example: Write a table
```
action: write_cells
spreadsheet_id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
range: "Sheet1!A1:C3"
values: [["Name","Email","Status"],["John","john@co.com","New"],["Jane","jane@co.com","Contacted"]]
```

## Example: Append leads
```
action: append_rows
spreadsheet_id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
range: "Sheet1"
values: [["New Lead","lead@example.com","2025-03-09"]]
```
