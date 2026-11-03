# Private Google Drive reader: setup and upload guide

This guide connects the dashboard to one private Google Drive folder. You do every step yourself. Never paste the service-account key into a chat, email, GitHub or this repository.

`<private folder>` below means your private data folder on the laptop: outside this project, outside Git and outside any cloud-synced folder. It holds the imported snapshots, the export folders and the reader key.

Start with the **synthetic test files**. They contain only made-up records, and the dashboard labels them SYNTHETIC TEST SNAPSHOT. Do not upload a real snapshot until the hosted app has restricted viewer access (milestone 4).

## How it works

```text
Laptop                                  Private Drive folder           Dashboard (reader)
import CSVs -> export generation  --->  nse_snapshot_<generation>.sqlite  ---> downloads with a Viewer-only
               (you upload by hand)     current_manifest.json                 service account, checks size and
                                                                              SHA-256, opens read-only, then shows it
```

- **Generation:** each export is a new, immutable generation. Its file name includes the generation ID, so a new upload never overwrites an old file.
- **Manifest:** `current_manifest.json` is the pointer. It names one generation and records that file's size, SHA-256, row counts, source times and quality totals. It contains no individual records.
- **Checks before activation:** the dashboard activates a generation only after the download matches the manifest's size and checksum, the database opens read-only, and its contents match the manifest.
- **Failures:**
  - A previously verified generation stays on screen, marked **STALE**, with the reason.
  - If there is none (for example, just after the server restarts), the page says **DRIVE SNAPSHOT UNAVAILABLE**.
  - It never shows demo or made-up figures in place of the snapshot.
  - Nothing is kept on the server's disk.
- **Checking for updates:** the dashboard checks Drive every 5 minutes, or when you click **↻ Check Drive now**. It downloads the snapshot only when the manifest names a different generation.

## Two kinds of permission (keep them separate)

| | Google Cloud project permissions (IAM) | Drive sharing |
|---|---|---|
| Controls | Who can manage the Cloud project: APIs, service accounts, keys | Who can open the folder and its files |
| What the reader needs | **None.** Skip "Grant this service account access to project" | **Viewer** on the `NSE Dashboard Data` folder only |
| Never | Give the service account Owner, Editor or any project role | Set the folder to "Anyone with the link", or give the reader Editor |

A project role does not let the service account read your Drive files, and Drive sharing does not give it any project permissions. The read-only scope (`drive.readonly`) limits what the app *asks* for. Drive sharing decides what it can *see*.

## Part A: Create the private folder

1. Open https://drive.google.com and click **New → New folder**. Name it `NSE Dashboard Data`.
2. Right-click the folder and choose **Share → Share**. Under **General access**, it must say **Restricted**.
3. Open the folder. Its address looks like `https://drive.google.com/drive/folders/1AbC…xyz`. The part after `folders/` is the **folder ID**. You will need it in Part D.

## Part B: Create the reader identity in Google Cloud

1. Open https://console.cloud.google.com. At the top, use the project picker: **New project**, name it `NSE Dashboard Reader`, then **Create**. Make sure it is the selected project.
2. Go to **APIs & Services → Library**, search for **Google Drive API**, open it, and click **Enable**.
3. Go to **IAM & Admin → Service accounts → + Create service account**.
   - Name: `dashboard-reader`. Click **Create and continue**.
   - On **Grant this service account access to project**, choose no role and click **Continue**. Then click **Done**.
4. Copy the service account's email address. It looks like `dashboard-reader@<project-id>.iam.gserviceaccount.com`.
5. Open the service account, then **Keys → Add key → Create new key → JSON → Create**. A `.json` file downloads.
   - This file is a password. Move it to `<private folder>\drive-reader\`. Do not put it in the project folder, OneDrive, email or chat.
   - If Google says key creation is disabled by an organization policy, stop and note the exact message. Do not turn the policy off.

## Part C: Share the folder with the reader (Viewer only)

1. In Drive, right-click `NSE Dashboard Data` and choose **Share → Share**.
2. Paste the service-account email from Part B, step 4, and set the role to **Viewer**.
3. Untick **Notify people**, because a service account has no inbox. Click **Share**.
4. Check that **General access** still says **Restricted**.

To revoke access later, open the same Share dialog and remove the service account. You can also delete its key in Cloud Console. The dashboard then shows the last verified generation as STALE until it restarts, and after that, UNAVAILABLE.

## Part D: Enter the secrets (you, not Claude)

The template is [`docs/streamlit-secrets.template.toml`](streamlit-secrets.template.toml). It contains placeholders only.

**For the local test on this laptop:**

1. Copy the template to `<private folder>\drive-reader\secrets.toml`. That folder is outside the project and outside OneDrive.
2. Open both `secrets.toml` and the downloaded key `.json` in Notepad.
3. Replace each `REPLACE…` value with the matching value from the JSON file:
   - **folder ID** from Part A, step 3.
   - **project_id, private_key_id, client_email, client_id:** copy each value between its quotes.
   - **private_key:** copy everything between its quotes, exactly. It is one long line containing `\n` sequences. Keep those as they are.
4. Save the file.

**For the hosted app (later, at milestone 4):** paste the same filled-in text into Streamlit Community Cloud under **your app → ⋮ → Settings → Secrets**. Do this only after the app is set to "Only specific people can view this app" and you have checked it in a signed-out browser window.

If anything still says `REPLACE`, the dashboard shows DATA SOURCE NOT CONFIGURED. It does not show demo data.

## Part E: Upload the synthetic test generation

1. Double-click `<private folder>\Build synthetic Drive test files.cmd`.
   - It prints the folder and the two file names, in upload order.
   - Files appear in `<private folder>\drive-test-synthetic\publish\<generation>\`.
2. In Drive, open `NSE Dashboard Data` and click **New → File upload**. Choose `nse_snapshot_<generation>.sqlite`. Wait until Drive says **Upload complete**.
3. Click **New → File upload** again and choose `current_manifest.json`.
4. Double-click `<private folder>\Open NSE dashboard (Drive).cmd`. It opens http://localhost:8504.
5. Confirm the following:
   - The page shows a yellow **SYNTHETIC TEST SNAPSHOT** banner that mentions Google Drive.
   - Source status is **SNAPSHOT**.
   - Recorded closed trades is **2**.

## Updating to a new generation

1. Export the new generation. Its folder contains a new `nse_snapshot_<new generation>.sqlite` and a new `current_manifest.json`.
2. Upload the new `.sqlite` file first. Wait for **Upload complete**.
3. Upload the new `current_manifest.json`. When Drive asks about the existing file, choose **Replace existing file**. That keeps one manifest and makes the new upload its latest version.
   - Alternatively, right-click the existing manifest and choose **File information → Manage versions → Upload new version**.
4. Click **↻ Check Drive now**, or wait up to 5 minutes. The generation ID in the banner changes.
5. Keep the previous `.sqlite` in Drive until the new generation shows. After that you may move older `.sqlite` files to **Bin**.

**What goes wrong and what you'll see:**

| Mistake | What the dashboard does |
|---|---|
| Manifest uploaded before the `.sqlite` finished | Keeps the previous generation (STALE). It retries every 5 minutes, and once the upload completes it switches to the new one. |
| "Keep both" chosen, leaving two `current_manifest.json` | Refuses to choose between them and shows STALE. Move the older manifest to Bin. |
| Wrong or damaged `.sqlite` (checksum mismatch) | Rejects it and keeps the previous generation (STALE). Re-upload the file from the export folder. |
| Reader's Viewer access removed or key deleted | STALE while the server keeps running, UNAVAILABLE after a restart. |

**Rolling back:** upload an older generation's `current_manifest.json` with **Replace existing file**. Its `.sqlite` must still be in the folder.

## Real data (not yet)

The real snapshot is exported the same way:

```text
python -m producer.export_snapshot --private-dir <private folder>
```

The files go to `NSE-Workstation-private\publish\<generation>\`. Do not upload them until milestone 4's access checks pass.
