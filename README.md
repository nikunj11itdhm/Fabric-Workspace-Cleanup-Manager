# Fabric Workspace Cleanup Manager

A localhost Python web application for inspecting Microsoft Fabric workspaces,
assigning them to existing trial capacities, browsing artifacts, and performing
explicitly confirmed item cleanup without deleting the workspace.

> Python + Flask + Waitress | Microsoft Entra browser sign-in |
> CSV workspace import | Primary/secondary trial capacities | Confirmed cleanup

**Local administration utility, not a production-hosted service.** Capacity
assignment can move workspaces off paid capacities. Cleanup changes real Fabric
resources. Back up your data and review the selected workspace before confirming.

## Table of contents

- [Overview](#overview)
- [Key capabilities](#key-capabilities)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Step-by-step setup](#step-by-step-setup)
- [Microsoft Entra sign-in and permissions](#microsoft-entra-sign-in-and-permissions)
- [Prepare your workspace CSV](#prepare-your-workspace-csv)
- [Use the application](#use-the-application)
- [Local development](#local-development)
- [Repository structure](#repository-structure)
- [Validation](#validation)
- [Troubleshooting](#troubleshooting)
- [Limitations and deletion safety](#limitations-and-deletion-safety)
- [Publishing without credentials](#publishing-without-credentials)
- [Security notes](#security-notes)
- [Contributing](#contributing)
- [Microsoft references](#microsoft-references)

## Overview

Import workspace IDs from a CSV, inspect their current capacity, and perform
operations using the permissions of the signed-in Microsoft Entra user.
Authentication and imported data remain in server memory for the web session.

| Component | Purpose |
| --- | --- |
| Python / Flask | Local web UI, session handling, and operation endpoints |
| Waitress | Runs the server on `127.0.0.1:5000` |
| Azure Identity | Interactive browser sign-in and in-memory token refresh |
| Fabric REST client | Paginated workspace, item, and capacity requests |
| Vanilla JavaScript / CSS | Workspace selection, artifact tables, and confirmations |
| CSV upload | Supplies the workspace IDs to inspect |
| pytest / Ruff / Node.js tests | Backend, lint, formatting, and frontend validation |

This repository contains source code, tests, and placeholder CSV data. It does
not include real tenant IDs, workspace IDs, capacity IDs, credentials, or
screenshots of signed-in tenant data.

## Key capabilities

### Workspace and artifact inventory

- Import up to 500 unique workspace UUIDs from a UTF-8 CSV.
- Normalize and deduplicate IDs; reject invalid imports without partial changes.
- Display workspace names, capacity names, available SKUs, and trial status.
- Browse artifact names, types, and IDs for an individual workspace.
- Retrieve all API result pages instead of displaying only the first page.

### Primary and secondary trial-capacity assignment

- Enter your own existing **Primary capacity ID** and optional **Secondary capacity ID**.
- Leave workspaces already assigned to a recognized trial capacity unchanged.
- Validate the current capacity SKU and the target trial's active state.
- Attempt secondary after a definite primary failure.
- Verify assignment before reporting success.
- Report pending or uncertain outcomes without submitting overlapping fallback writes.

### Confirmation-protected workspace cleanup

- Preview items in the selected workspace before deletion.
- Require the exact phrase `DELETE <workspace UUID>`.
- Use a workspace/session-bound, single-use confirmation that expires in five minutes.
- Target the confirmed snapshot, not items subsequently added to the workspace.
- Report individual successes, permission failures, and unsupported items.
- Verify and display remaining inventory; retain the workspace itself.

### Same-session browser authentication

- Open Microsoft Entra sign-in directly in the browser, with no CLI step.
- Keep one in-memory credential per local web session.
- Reuse cached tokens or refresh silently for Fabric API calls.
- Prevent duplicate login submissions from opening repeated sign-in windows.
- Return an explicit error if interaction becomes necessary again.
- Refresh CSRF tokens before writes and block actions from changed sessions.

## Architecture

```text
Browser: http://localhost:5000
    |
    +-- Tenant ID -> Microsoft Entra browser sign-in / MFA
    |                   |
    |                   +-- Temporary localhost SDK callback
    |                   +-- Credential and token cache in server memory
    |
    +-- CSV upload -> Workspace IDs in the web session
    |
    +-- Flask + Waitress -> Fabric HTTPS REST APIs
            |
            +-- Admin workspace and item inventory
            +-- Capacity lookup and verified assignment
            +-- Item deletion after explicit confirmation
```

### Runtime flow

1. The server binds to loopback only; the user opens the local browser UI.
2. The user supplies a tenant ID and completes Microsoft browser sign-in.
3. The app binds the resulting identity to its local web session.
4. A CSV supplies the workspaces to inspect.
5. Inventory reads populate workspace and artifact tables.
6. Assignment and deletion run only after the user initiates and confirms them.
7. The UI shows per-operation results, including failures and unverified outcomes.
8. Sign-out closes the credential and removes the server-side session state.

## Prerequisites

| Requirement | Details |
| --- | --- |
| Operating system | Windows with an interactive desktop session |
| Python | Python 3.11-3.14; local validation used Python 3.14 |
| pip and venv | Included in a standard Python installation |
| Git | Required for the clone workflow; ZIP download is an alternative |
| Browser | Microsoft Edge or Google Chrome on the server computer |
| Microsoft account | Work/school account with the required Fabric permissions |
| Fabric administrator | Required by the tenant-wide admin inventory endpoints |
| Trial capacities | Existing active trial capacity IDs supplied by the user |
| Network access | Microsoft Entra sign-in, Fabric APIs, and Python package feeds |
| Node.js | Optional for running frontend development tests; not needed to run the app |

You do **not** need Azure CLI, Fabric CLI, Docker, a database, a client secret,
or a custom application client ID to try the development browser-login mode.
Your tenant must still allow the SDK's Microsoft development application and
its Fabric access. See [sign-in and permissions](#microsoft-entra-sign-in-and-permissions).

## Step-by-step setup

### 1. Install Python

1. Open [Python downloads for Windows](https://www.python.org/downloads/windows/).
2. Choose a standard 64-bit installer for a supported Python 3.11-3.14 release.
   Do not choose the embeddable distribution.
3. Run the installer. If offered, select **Add python.exe to PATH** and ensure
   **pip** and the **Python launcher** are installed.
4. Close and reopen PowerShell after installation.
5. Confirm the installed version:

```powershell
py --version
py -m pip --version
```

The commands below use the Windows Python launcher, `py`. If your installation
provides `python` instead, replace `py` with `python` for creating the environment.
If several Python versions are installed, select one explicitly, for example
`py -3.14 -m venv .venv`.

### 2. Install Git and obtain the source

Install [Git for Windows](https://git-scm.com/download/win) if needed. Reopen
PowerShell and confirm:

```powershell
git --version
```

Clone this repository into a folder of your choice:

```powershell
git clone https://github.com/nikunj11itdhm/Fabric-Workspace-Cleanup-Manager.git
Set-Location .\Fabric-Workspace-Cleanup-Manager
```

Alternatively, choose **Code > Download ZIP** on GitHub, extract the archive,
and open PowerShell in the extracted folder.

### 3. Create an isolated Python environment

```powershell
py -m venv .venv
```

All remaining commands call the virtual environment's Python directly.
Activation is optional, and no PowerShell execution-policy changes are needed.

### 4. Install the required Python libraries

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

| Library | Purpose |
| --- | --- |
| `Flask` | Web routes, templates, cookies, and session integration |
| `azure-identity` | Microsoft Entra interactive browser credential |
| `requests` | Fabric HTTPS REST API calls |
| `waitress` | Local application server |

The installer also resolves these libraries' dependencies. Do not copy someone
else's `.venv` folder or install individual packages globally to run this app.

### 5. Start the application

From the repository folder:

```powershell
.\.venv\Scripts\python.exe app.py
```

Expected output includes:

```text
Fabric Workspace Manager: http://localhost:5000
Serving on http://127.0.0.1:5000
```

Keep that PowerShell window open while using the app. Open:

```text
http://localhost:5000
```

The application does not create, purchase, or renew a Fabric trial.

### 6. Sign in

1. Enter your **Microsoft Entra tenant ID** (directory UUID).
2. Click **Sign in with Microsoft in browser**.
3. Complete Microsoft sign-in, consent if required, and MFA in the browser tab
   opened by the SDK.
4. Return to the app tab; it detects completion automatically.
5. Continue using the same browser profile and `http://localhost:5000` origin.

No client ID or secret is entered in the UI. Organizational consent and access
requirements still apply.

### 7. Prepare and import workspace IDs

Create your own CSV using the format in
[Prepare your workspace CSV](#prepare-your-workspace-csv). Choose the file and
click **Import and inspect**.

The bundled `sample-workspaces.csv` contains placeholder UUIDs, not working
workspaces. Replace them with your own IDs before expecting inventory results.

### 8. Stop and start again

1. Click **Sign out** in the UI.
2. Press **Ctrl+C** in the server's PowerShell window.

To run it again:

```powershell
Set-Location .\Fabric-Workspace-Cleanup-Manager
.\.venv\Scripts\python.exe app.py
```

The `Set-Location` example assumes you are in the repository's parent folder;
otherwise use the actual path where you cloned it. Restarting clears all
in-memory sessions. Reload old tabs and sign in again.

## Microsoft Entra sign-in and permissions

The official Azure Identity SDK uses `InteractiveBrowserCredential` without a
custom client ID, so authentication uses a Microsoft development application.
Microsoft documents this default as a development scenario, not a recommended
production configuration.

| Operation | Access required |
| --- | --- |
| Browser sign-in | Account allowed by the tenant, consent, and MFA/Conditional Access |
| Tenant inventory | Fabric administrator role for the admin endpoints |
| Capacity details | Contributor or Administrator access to the capacity |
| Workspace reassignment | Workspace Admin and target capacity Contributor/Admin |
| Item cleanup | Item/workspace write permissions for the particular item |

Fabric administrator is a **tenant role**, not an authentication method, and
does not automatically grant every workspace or capacity mutation permission.

The APIs also require appropriate delegated permissions, including
`Tenant.Read.All`, `Workspace.ReadWrite.All`, `Capacity.ReadWrite.All`, and
`Item.ReadWrite.All`, as applicable. The app requests the Fabric `.default`
resource scope through the SDK; this does not create or grant missing consent.
An administrator may have to approve the development application, or your tenant
may require a separately registered application instead.

The SDK starts a temporary localhost callback listener, normally on a free port
between 8400 and 8999. Sign-in times out after five minutes. It must run on the
same interactive desktop as the browser.

API calls do not automatically open sign-in windows. If Microsoft requires
interaction again, sign out and explicitly sign back in. The application cannot
bypass organization policy, MFA, API permissions, or service throttling.

## Prepare your workspace CSV

Use UTF-8 encoding and one workspace UUID per row:

```csv
workspace_id
11111111-1111-4111-8111-111111111111
22222222-2222-4222-8222-222222222222
```

These are placeholder values. Find real IDs in your Fabric workspace URL or
workspace settings.

| Rule | Behavior |
| --- | --- |
| Header | `workspace_id`, `WorkspaceId`, or `Workspace ID` |
| Values | Valid, nonempty workspace UUIDs; not workspace names or URLs |
| Duplicates | Normalized and removed |
| Invalid row | Entire upload rejected; previous import retained |
| Extra columns | Ignored |
| Encoding | UTF-8, with or without a byte-order mark |
| Limits | 500 unique workspace IDs; 2 MB maximum request size |
| Capacity IDs | Entered in the UI, not read from the CSV |

In Excel, use **Save As > CSV UTF-8 (Comma delimited)**. A semicolon-separated
file is not the expected format.

Keep real tenant inventories local. The repository ignores `*.csv` files except
the bundled placeholder sample to help prevent accidental publication.

## Use the application

### Inspect workspaces and artifacts

1. Sign in and import your CSV.
2. Review workspace names, capacity information, and trial classification.
3. Click **Artifacts** on a workspace to fetch its item names, types, and IDs.
4. Use **Refresh inventory** when you need updated workspace metadata.

Capacity inventory includes only capacities where you have Contributor/Admin
access. An inaccessible capacity is shown as unknown, not assumed to be non-trial.

### Assign selected workspaces to a trial

1. Enter an existing active trial's **Primary capacity ID**.
2. Optionally enter a different **Secondary capacity ID**.
3. Select the workspaces to process.
4. Click **Assign selected to trial** and review the confirmation warning.
5. Review each workspace's result before retrying.

Recognized trial SKUs are `FT1` and `FTL<number>`. A workspace already on a
recognized trial is skipped. Non-trial assignments, including paid-capacity
assignments, are moved only after your confirmation.

Secondary is attempted after a definite primary failure. Accepted assignments
are polled for up to 90 seconds per target. An uncertain write, a pending
operation, or a verification timeout does not trigger overlapping fallback
writes. Refresh and verify the actual assignment before retrying.

### Delete items while retaining the workspace

1. Back up required data and review dependencies.
2. Click **Delete items** for exactly the intended workspace.
3. Review the merged Core/admin item preview.
4. Type the exact displayed phrase: `DELETE <workspace UUID>`.
5. Click **Delete listed items**.
6. Review per-item results and the remaining inventory.

Only previewed item IDs are directly targeted. New items are not added to the
deletion snapshot. Deleting a parent artifact can still affect related or
system-managed children, so review dependencies before confirming.

The confirmation is single-use, expires after five minutes, and is bound to the
workspace and session. The app never deletes the workspace itself as a workaround
for an unsupported item.

## Local development

| Command | Purpose |
| --- | --- |
| `py -m venv .venv` | Create the isolated environment |
| `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` | Install runtime libraries |
| `.\.venv\Scripts\python.exe app.py` | Start the local app |
| `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` | Install test/lint libraries |
| `.\.venv\Scripts\python.exe -m pytest -q` | Run backend tests |
| `.\.venv\Scripts\python.exe -m ruff check .` | Lint Python |
| `.\.venv\Scripts\python.exe -m ruff format --check .` | Check Python formatting |
| `node --check .\static\app.js` | Check frontend JavaScript syntax |
| `node --test .\tests\frontend.test.cjs` | Run frontend tests |

No npm install, frontend build, database migration, or `.env` file is needed.
Node.js is only needed for development validation.

## Repository structure

```text
Fabric-Workspace-Cleanup-Manager/
|-- app.py                    # Web routes, sessions, CSRF, operation handlers
|-- browser_auth.py           # Session-bound browser credential
|-- fabric.py                 # CSV parsing and Fabric API operations
|-- requirements.txt          # Runtime Python libraries
|-- requirements-dev.txt      # Runtime + pytest and Ruff
|-- sample-workspaces.csv     # Placeholder workspace IDs only
|-- templates/
|   `-- index.html            # Login and management screens
|-- static/
|   |-- app.js                # UI actions and token refresh
|   `-- style.css             # Responsive styling
|-- tests/
|   |-- test_app.py           # Routes, sessions, CSRF, confirmations
|   |-- test_browser_auth.py  # Credential reuse and authentication errors
|   |-- test_fabric.py        # Pagination, fallback, CSV, cleanup
|   `-- frontend.test.cjs     # Upload and stale-session frontend behavior
|-- .gitignore
`-- README.md
```

## Validation

Install development libraries first:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pip check
node --check .\static\app.js
node --test .\tests\frontend.test.cjs
```

Backend tests cover CSV validation, pagination, trial assignment fallback,
unknown outcomes, confirmed deletion, session isolation, consent errors, and
silent-only token acquisition. Frontend tests cover fresh-token multipart
uploads, changed sessions, JSON submission, and avoiding mutation replay.

Tests use mocks where authentication or Fabric resources would otherwise be
required. They do not authenticate a tenant or mutate Fabric resources.

For a running server, a basic health check is:

```powershell
Invoke-RestMethod http://localhost:5000/health
```

Expected response: `status` is `ok`. A healthy local server does not prove that
your account has all necessary Fabric permissions.

## Troubleshooting

### Python or `py` is not recognized

Reopen PowerShell after installation. Check `py --version` and
`python --version`. Use the command available on your machine, or run the
installed Python executable by its full path. Ensure it is a supported version.

### `No module named flask`, `azure`, or another library

Use the virtual environment's Python for both installation and execution:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

### PowerShell blocks `Activate.ps1`

Activation is not required. Run `.\.venv\Scripts\python.exe` directly as shown
above. Do not weaken your execution policy just to activate the environment.

### Port 5000 is already in use

Identify the listening process:

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

Stop only the previous app instance you own, preferably with **Ctrl+C** in its
terminal. Do not stop unrelated processes. The current app uses a fixed port.

### Browser sign-in does not finish or keeps asking to sign in

Use the app in the same browser profile and at `http://localhost:5000`.
Complete MFA and return to the original app tab. Repeated clicks reuse the
existing flow. If it times out, retry explicitly after the error.

If Microsoft reports consent or an `AADSTS` error, resolve the tenant policy or
application approval with your administrator. The SDK's development application
may not be permitted by your organization. Restarting the server clears sessions
and requires a new sign-in.

### `Invalid CSRF token` or the page says the session changed

Use **Reload current session**, or refresh the app page, then sign in if prompted.
Reselect the CSV and retry. The app refreshes the CSRF token before writes and
uses a dedicated cookie so unrelated localhost Flask apps do not overwrite it.
Changed or expired sessions block the action; no mutation is silently replayed.

### CSV imports but workspaces are missing

Check that the CSV contains real workspace UUIDs for the signed-in tenant, not
the sample placeholders. Confirm Fabric administrator access and that the
workspace is active.

### `RequestBlocked`, HTTP 429, or an upstream-service block

Respect the returned `Retry-After` header or the service's **blocked until**
timestamp. If the timestamp says UTC, convert it to your local time before
retrying. Do not repeatedly click refresh, artifact lookup, or delete.

The current implementation retries short numeric HTTP 429 delays of up to
30 seconds, with a bounded number of attempts. Longer blocks and other upstream
errors are surfaced to you. There is **no fixed gap between successful deletes
and no whole-batch cooldown scheduler**. A failed item can be followed by the
next item, so use small cleanup batches and wait for the service block to end
before retrying a failed operation. Signing in again does not clear API limits.

### Capacity status is unknown or assignment fails

Verify the supplied target is an existing active trial capacity, not its name
or an Azure resource ID. Ensure workspace Admin and capacity Contributor/Admin
permissions. The app will not guess whether an inaccessible source is a trial.

### Some items remain after cleanup

Generic item deletion is not supported by every artifact type. System-managed
children, dependencies, legacy Power BI artifacts, and permission restrictions
can also prevent cleanup. Review each result, resolve the underlying condition,
and use supported Fabric tools for remaining artifacts where necessary.
Admin inventory can briefly lag behind completed deletions.

## Limitations and deletion safety

- Local desktop administration only; do not expose the server publicly.
- Tenant-wide admin endpoints are preview/rate-limited where documented.
  Consult the API references for current quotas.
- Capacity inventory is limited to the caller's access.
- The app does not create trials or bypass capacity eligibility or permissions.
- Items are deleted sequentially, not in parallel. There is no guaranteed
  service-wide rate budget or fixed inter-item cooldown.
- A second pass retries explicit dependency/validation conflicts after other
  items are removed; transport-uncertain writes are not blindly retried.
- The default Fabric delete behavior is used. Supported types may be
  soft-deleted; there is no force-permanent-delete option or recovery guarantee.
- Workspace folders and the workspace itself are retained.
- A failed final inventory read is reported as unverified, not as empty.
- Sign-out/restart clears in-memory workspace imports and authentication.
- Microsoft browser SSO is not signed out globally when this app signs out.

## Publishing without credentials

The ignore rules exclude virtual environments, caches, local environment files,
logs, screenshots, and real CSV files. Only `sample-workspaces.csv` is allowed
as the bundled placeholder CSV.

Before publishing changes:

```powershell
git status --short
git diff --cached --stat
git diff --cached
git check-ignore -v .venv .env workspaces.csv
```

Confirm no real identifiers, inventories, browser screenshots, customer data,
passwords, tokens, or generated state are included. Keep additional local data
in the ignored `local-data` folder if needed.

Use GitHub CLI or Git Credential Manager for GitHub authentication. Never paste
GitHub tokens into source files, command scripts, or Git history.

## Security notes

- The server binds only to loopback and checks allowed hosts and local callers.
- One signed, HttpOnly, SameSite session cookie identifies each server session.
- Auth tokens stay server-side in memory; no token-cache persistence is configured.
- The browser never receives Fabric access or refresh tokens.
- CSRF and same-origin checks protect writes.
- A content security policy restricts browser resources to the local app.
- Deletion confirmations are exact, time-limited, session-bound, and single-use.
- No permissions, tenant policies, MFA requirements, or throttling controls are bypassed.
- HTTPS is used for Microsoft identity and Fabric calls; plain HTTP is used only
  for the local UI and temporary loopback identity callback.

## Contributing

1. Create a branch from `main`.
2. Keep credentials, tenant data, and generated state out of Git.
3. Make focused changes and add tests for changed behavior.
4. Run the validation commands.
5. Open a pull request describing the behavior, limitations, and validation.

## Microsoft references

- [InteractiveBrowserCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.interactivebrowsercredential)
- [Admin: list workspaces](https://learn.microsoft.com/en-us/rest/api/fabric/admin/workspaces/list-workspaces)
- [Admin: list items](https://learn.microsoft.com/en-us/rest/api/fabric/admin/items/list-items)
- [Core: list capacities](https://learn.microsoft.com/en-us/rest/api/fabric/core/capacities/list-capacities)
- [Core: assign workspace to capacity](https://learn.microsoft.com/en-us/rest/api/fabric/core/workspaces/assign-to-capacity)
- [Core: list items](https://learn.microsoft.com/en-us/rest/api/fabric/core/items/list-items)
- [Core: delete item](https://learn.microsoft.com/en-us/rest/api/fabric/core/items/delete-item)
- [Fabric API throttling](https://learn.microsoft.com/en-us/rest/api/fabric/articles/throttling)
