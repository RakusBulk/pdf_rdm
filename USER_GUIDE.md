# User Guide — pdf-drm

This document covers full usage of the system, following the real-world
flow: **encrypt a document → manage it on the server → open it with the
viewer**. See [README.md](README.md) for technical details (architecture,
setting up a dev environment, packaging the apps). *(Note: README.md is
currently in Vietnamese only.)*

## System overview

Three components:

| Component | Role |
|---|---|
| **Server** (`server/`) | Stores decryption keys, decides which machine can open which document and until when. Has a web admin UI + API. |
| **CLI** (`cli/`) | Command-line tools for admins: bulk encryption, bulk license grants — for when doing it through the web UI would be too slow (e.g. 100 documents × 100 people). |
| **Viewer** (`viewer/`) | App installed on end-user machines; opens `.cpdf` files after checking in with the server. |

Flow: admin encrypts a PDF → produces a `.cpdf` file (useless without a key
from the server) → distributes the file to everyone → each machine that
wants to open it must be approved by the admin (via the web dashboard or
CLI) → the viewer requests the key from the server every time it opens the
file, and the server checks the machine + expiry before granting it.

---

## Part 1 — Creating a `.cpdf` file (encrypting a document)

Two ways: **via the web** (fast, nothing to install, recommended for one-off
files) or **via the CLI** (better for bulk encryption or scripted/automated
runs).

### Option A — Via the web dashboard (nothing to install)

Go to the **Documents** tab, "Encrypt & upload new document" section: enter
a document name, choose a PDF file, click "Encrypt & upload". The server
encrypts it itself (AES-256-GCM) and **keeps the `.cpdf` file stored on the
server** — the document shows up immediately in the list, with buttons:

- **Download** — download the `.cpdf` file to distribute to users.
- **Delete** — permanently remove the document (and every license granted
  for it) from the system; cannot be undone.

The **Size** column shows how much storage the file takes up on the
server — useful for tracking total storage in use ("manage/monitor files").
Max file size is 300MB.

> Documents created via the CLI (Option B) are **not** kept on the server
> (only the key is) — the "Download" button is disabled for those, since
> the `.cpdf` file only exists on the machine that ran the encrypt command.

### Option B — Via the CLI (better for bulk encryption)

#### Setting up the environment

```bash
cd pdf-drm
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

#### Encrypting a single file

```bash
python -m cli.encrypt path/to/report.pdf --title "Q3 Report" \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN \
  --out path/to/report.cpdf
```

- `--out` is optional — defaults to the same filename with a `.cpdf`
  extension.
- The command prints a `doc_id` — used later to grant access.
- **Never re-run this command on a file you've already granted licenses
  for** — each run generates a new `doc_id` and key, making every previously
  granted license meaningless.

#### Bulk encryption (many files at once)

```bash
python -m cli.batch_encrypt ./pdfs_to_share --out-dir ./encrypted \
  --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Scans every `*.pdf` in the folder (`--recursive` to include subfolders),
encrypts each one, and writes a `manifest.csv` (columns `source_pdf, title,
doc_id, cpdf_path`) — used in Part 2 for bulk grants/group creation.

After this step: distribute the `./encrypted` folder (the same set of files
for everyone, over email/Google Drive/USB/whatever) along with a link to
download the viewer.

---

## Part 2 — Server features

### 2.1 Setup & startup

```bash
export PDF_DRM_ADMIN_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
./scripts/run_server.sh
```

`PDF_DRM_ADMIN_TOKEN` is the full-access "bootstrap" key — always valid,
used to log in the first time and create admin accounts. **Run the server
behind real HTTPS/TLS in production** — decryption keys and passwords
travel over this API.

### 2.2 Logging into the dashboard

Open `http://<server-address>:8443/admin/ui/`. Two ways to log in:

1. **Account (username/password)** — for everyday use. Create the first
   account in the **Admins** tab (requires logging in with the token once
   first). Logging in with an account returns a 24-hour session.
2. **Admin token** — for bootstrapping, or when you've forgotten every
   account password (there's no "forgot password" via email).

Leave the "Server URL" field **empty** if you're viewing the dashboard from
that same server (the common case) — only fill it in when the dashboard is
opened from a different address than the server. If you accidentally type
the token into this field, the app will show an error immediately instead
of silently calling the wrong API.

### 2.3 Dashboard tabs

**Documents** — encrypt/upload PDFs directly (see Part 1, Option A), and
list every encrypted file (title, doc_id, size, **groups it belongs to**,
number of licenses, creation date). The "Groups" column shows a badge for
each group the document belongs to (if any) — click a badge to jump to the
Groups tab and see its detail right away. Each row has "View licenses"
(jumps to the Licenses tab, pre-filtered), "Download" (download the
`.cpdf` — only available for documents created via the web), and "Delete"
(permanently removes the document + all related licenses, cannot be
undone).

**Document Groups** — bundle multiple documents into a named group, to
grant access in bulk instead of picking documents one by one:
- "Create new group": name it + tick which documents to include.
- Click "View" on a group to view/add/remove documents, or "Delete this
  group" (only deletes the group, not licenses already granted through it).
  The detail panel has a "Hide" button to close it.
- **Adding a new document to a group that already has members with access →
  those members are automatically granted access to the new document too**,
  no manual re-grant needed. The system determines "already a group member"
  by checking who currently holds an active license for at least one other
  document in the group; the auto-grant's expiry matches that person's
  existing expiry within the group (it won't extend or shorten it).
- "Grant license by group": pick a group + machine fingerprint + expiry +
  display name → grant every document in the group to one machine in a
  single click.

**Pending Requests** — where self-service requests sent automatically from
the viewer are handled (see Part 3). Each row shows **Username, Email,
Fingerprint, Note, Submitted at**. Approval flow:
1. Tick one or more requests (row checkbox, or "select all" in the header).
2. Choose which documents to grant — tick individual boxes, or pick a
   **group** from the "Apply group" dropdown and click "Apply" to
   auto-tick every document in that group.
3. Enter a **number of days** (e.g. `30`) to auto-compute the expiry date,
   or pick a specific date/time yourself.
4. Click "Approve selected" (grants access) or "Reject selected".

**Licenses** — grant/revoke licenses directly, per document:
- "Grant license directly": pick a document + type in a fingerprint + set
  expiry + display name. Granting again for a fingerprint that already has
  a license merges into the same person instead of creating a separate row.
- License list table: when filtering by "All" documents, the table
  **groups by person (machine_fingerprint)** — one row per person with a
  document count, click **"Detail"** to see a panel below listing each
  document that person was granted (title, expiry, status, its own Revoke
  button), with a "Hide" button to close the panel. When filtered to one
  specific document, the table shows a flat list as before (context is
  already clear, no need to group).

**Access Log** — history of every time the viewer requested a key
(including rejections), filterable by document — use it to check who
opened what and when, or to investigate a suspected leak.

**Admins** — create a new admin account (username + password, minimum 8
characters) or delete an account (automatically invalidates that account's
login sessions).

### 2.4 Doing the same things via the CLI (for bulk/automated operations)

```bash
# print this machine's fingerprint
python -m cli.manage_license fingerprint

# grant / revoke / list licenses for one document
python -m cli.manage_license grant  --doc-id <id> --machine-fingerprint <fp> --expires-at <ISO> [--label ...] --server ... --admin-token ...
python -m cli.manage_license revoke --doc-id <id> --machine-fingerprint <fp> --server ... --admin-token ...
python -m cli.manage_license list   [--doc-id <id>] --server ... --admin-token ...
python -m cli.manage_license log    [--doc-id <id>] --server ... --admin-token ...

# manage groups
python -m cli.manage_groups create     --name "..." [--manifest manifest.csv | --doc-id <id> ...] --server ... --admin-token ...
python -m cli.manage_groups list       --server ... --admin-token ...
python -m cli.manage_groups show       --group-id <id> --server ... --admin-token ...
python -m cli.manage_groups add-docs   --group-id <id> [--manifest ... | --doc-id ...] --server ... --admin-token ...
python -m cli.manage_groups remove-docs --group-id <id> --doc-id <id> ... --server ... --admin-token ...
python -m cli.manage_groups delete     --group-id <id> --server ... --admin-token ...
python -m cli.manage_groups grant      --group-id <id> --machine-fingerprint <fp> --expires-at <ISO> [--label ...] --server ... --admin-token ...

# approve self-registration requests
python -m cli.approve_pending list        [--status pending|approved|rejected] --server ... --admin-token ...
python -m cli.approve_pending approve     --request-id <n> [--manifest ... | --doc-id ... | --group-id <id>] --expires-at <ISO> [--label ...] --server ... --admin-token ...
python -m cli.approve_pending approve-all [--manifest ... | --doc-id ... | --group-id <id>] --expires-at <ISO> [--only-domain company.com] --server ... --admin-token ...
python -m cli.approve_pending reject      --request-id <n> --server ... --admin-token ...

# grant N users × M documents in a single command (users.csv columns: machine_fingerprint,label)
python -m cli.batch_grant [--manifest manifest.csv | --doc-id <id> ... | --group-id <id>] \
  --users users.csv --expires-at <ISO> --server ... --admin-token ...
```

`--expires-at` uses ISO-8601 format, e.g. `2026-12-31T23:59:59+00:00`.

---

## Part 3 — Viewer features

### 3.1 Installation

Download the `.zip` for your OS (see [README.md](README.md) section 6 if
you need to build it yourself), unzip it, and run it directly — **no
Python install required**.

- **macOS**: the app isn't code-signed, so the first launch must be
  **right-click the app → Open → Open**, instead of a normal double-click
  (double-clicking gets blocked by Gatekeeper with a "cannot verify
  developer" message).
- **Windows**: SmartScreen will warn "Windows protected your PC" — click
  "More info" → "Run anyway".

### 3.2 First run — registering the machine

On first launch, a dialog asks for 3 pieces of information:

- **Server URL** — the address the admin gave you.
- **Your name** — display name (the admin will see this when reviewing
  requests, and it also appears in the watermark on every page).
- **Your email** — so the admin can contact/verify you.

After filling these in and clicking OK, the app **immediately sends a
registration request to the server** — you don't need to know in advance
which document you'll open. A confirmation dialog appears with the request
number. You just need to ask the admin to approve it (or the admin will
notice it in the "Pending Requests" tab and approve it themselves).

This information is saved to `~/.pdf_drm_viewer/config.json` on your
machine — **you're only asked once**; subsequent launches reuse it without
asking again.

### 3.3 Opening a document

Click **Open...** on the toolbar and select a `.cpdf` file. If the machine
has already been granted access, the document opens right away. If not (or
access has expired/been revoked), the app explains why and asks if you'd
like to **send an access request for this specific document** (reusing the
name/email already saved, no need to re-enter them).

The toolbar has:
- **Prev / Next** — switch pages.
- **Zoom + / Zoom −** — zoom in/out.
- **Bookmarks** — toggle the table-of-contents panel on the left (if the
  source PDF has one); click an entry to jump straight to that page.
- **Page: [number field]** — type a page number and press Enter to jump
  straight there, instead of clicking Next/Prev repeatedly.
- **Search** (or Cmd+F/Ctrl+F) — opens the search bar; type a term and
  press Enter to search **the entire document** (not just the current
  page). "Previous"/"Next" buttons step between results, a counter shows
  the current match (e.g. "2/5"), and it automatically jumps to the right
  page and **highlights the match in yellow**. Click "Close" to dismiss
  the search bar.

You can also **scroll with the mouse wheel/trackpad** to move between
pages directly — scrolling past the bottom of the current page
automatically advances to the next one (and the reverse at the top of a
page), no need to click Next/Prev.

> **On searchability:** a `.cpdf` file on disk cannot be searched by any
> tool other than this viewer — that's the whole point of encrypting it.
> Once the viewer decrypts it into memory, search works over the PDF's
> full original text content, with no limitations compared to a regular
> PDF.

There is **deliberately no** Print / Save / Export / Save As button
anywhere in the app — that omission is exactly how printing/export is
blocked.

The status bar at the bottom shows: current page, remaining license time,
and a "Printing/export disabled" reminder.

### 3.4 Anti-capture watermark

Every visible page shows a bold red watermark at 2 diagonal positions,
containing the **user's name + machine ID + real-time clock**, which shifts
position and rotation roughly every 1.6 seconds. Purpose: if someone
screenshots or photographs the screen, the person and time are still
identifiable.

### 3.5 When a document auto-closes

The app re-checks with the server every 5 minutes; if access has been
revoked or has expired, the open document **closes immediately** with a
message explaining why.

If the server can't be reached (network down): the app shows an "Offline"
warning right away, then **retries every 30 seconds** (faster than the
normal cadence) to quickly detect when the connection comes back. If it's
still unreachable after **5 minutes**, the document **actually closes** with
a clear explanation — no more silent warnings while the document stays open
indefinitely, as before. If the connection recovers in the meantime, the
app goes back to its normal check cadence without needing to reopen the
document.

### 3.6 Mandatory encrypted connection (HTTPS)

The app **requires** the Server URL to use `https://` — if you enter
`http://` (except for `localhost`/`127.0.0.1`, used for development/testing),
the app refuses to save it and asks you to re-enter it. Reason: the
document's decryption key travels over this connection every time the file
is opened — over plain HTTP on a LAN/public Wi-Fi, anyone sniffing traffic
on the same network could read the key. When deploying a real server (not
localhost), set up real HTTPS (a TLS certificate) — see the "Operational
Security" section of README.md.

---

## Part 4 — Full workflow example

```bash
# 1. Encrypt all Q3 reports
python -m cli.batch_encrypt ./q3_reports --out-dir ./encrypted \
  --manifest ./manifest.csv --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

# 2. Bundle them into one group
python -m cli.manage_groups create --name "Q3 Reports" --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
# -> prints out a group_id

# 3. Distribute the ./encrypted folder + a link to download the viewer to everyone

# 4. Users launch the viewer for the first time -> automatically sends a request to the server

# 5. Admin goes to the "Pending Requests" tab on the web, selects the "Q3 Reports" group,
#    enters a number of days (e.g. 30), and bulk-approves the valid requests
```

---

## Security limitations to know about

No DRM (including commercial products) can stop 100% of users who have
admin rights on their own machine. This system **cannot** prevent:

- Screenshots / photographing the screen with a phone (the watermark helps
  *trace*, it does not *prevent*).
- Virtual printers or OS-level full-screen capture tools.
- A user with admin rights dumping the app's memory with a debugger while
  it's running.
- Changing the system clock between two server re-checks (5 minutes by
  default).
- Reverse-engineering the viewer build (it runs on the user's machine, so
  it can always be analyzed).

If the requirement is "absolutely no leaks possible," no software solution
can deliver that — only reduce the risk and improve traceability.

---

## Common troubleshooting

| Symptom | Common cause | Fix |
|---|---|---|
| Dashboard login fails, server log shows a strange path like `/admin/ui/<token>/admin/...` | The admin token was typed into the "Server URL" field by mistake | Leave the Server URL field empty when using the same page as the server; only enter the token in the "Admin token" field |
| `python -m cli...` raises `SyntaxError` or a missing module error | The venv wasn't activated (`source .venv/bin/activate`); running the system Python instead | Activate the venv in the terminal you're running the command from |
| Viewer says "License denied" even though access was just granted | Opening a stale `.cpdf` file that was re-encrypted (new `doc_id`) | Re-grant access using the latest `doc_id`, or use the `.cpdf` file that access was actually granted for |
| Forgot every admin account password | There's no email-based recovery | Log in again with `PDF_DRM_ADMIN_TOKEN` and create a new account in the Admins tab |
| macOS/Windows blocks the viewer from opening | The app isn't code-signed | macOS: right-click → Open → Open. Windows: "More info" → "Run anyway" |
