NSE SNAPSHOT PUBLISHER - READ ME FIRST
=======================================

WHAT THIS IS
------------
A small, separate tool that makes a read-only copy ("snapshot") of six CSV files your trading
application already writes. Mihir's private dashboard displays that snapshot.

What it does:
  - Reads the six CSV files named in publisher_config.ini.
  - Checks them, then writes a new snapshot file and a small manifest file into its own output folder.
  - You then upload those two files by hand to a private Google Drive folder that Mihir shares with you.

What it does NOT do:
  - It does not change, move, lock or delete your CSV files.
  - It does not touch your trading application, its settings or its broker login.
  - It does not log in to any broker, place orders, or connect to the internet.
  - It does not run in the background, start with Windows, or upload anything by itself.
  - This package contains no data and no passwords or keys.

Every run creates a NEW snapshot. Old ones are never overwritten. If a run fails for any reason,
nothing new is created and the previous snapshot stays exactly as it was.

One limitation: the six files are checked one by one, but your trading application gives no
"all six files are finished" signal. So the tool cannot prove that all six files come from the same
moment. The reports say "cross-file consistency: UNVERIFIED" for that reason. That is expected.


1. INSTALL PYTHON (once)
------------------------
  a. Open https://www.python.org/downloads/windows/ and download Python 3.12 (64-bit installer).
  b. Run the installer. On the first screen, TICK "Add python.exe to PATH", then click "Install Now".
  c. No other software or packages are needed.

If Python is already installed, check it: press Windows+R, type cmd, press Enter, then type
    python --version
It should say Python 3.10 or newer (3.12 is what was tested).


2. PUT THIS FOLDER IN PLACE (once)
----------------------------------
  a. Unzip NSE-Snapshot-Publisher.zip somewhere simple, for example C:\NSE-Snapshot-Publisher
  b. Do NOT put it inside your trading application's folder, or inside a Google Drive, OneDrive or
     Dropbox folder.


3. SET THE PATHS (once)
-----------------------
  a. In the NSE-Snapshot-Publisher folder, copy publisher_config.example.ini and rename the copy
     to publisher_config.ini
  b. Open publisher_config.ini with Notepad.
  c. Under [sources], replace each C:\PATH\TO\... with the real location of that file.
     Tip: in File Explorer, hold Shift, right-click the file, choose "Copy as path", and paste.
  d. Under [output], choose a NEW empty folder for the results, for example
     C:\NSE-Snapshot-Publisher-output
     It must be separate from the trading application's folders. The tool refuses otherwise.
  e. Save the file.


4. CHECK THE INSTALLATION (once)
--------------------------------
Double-click "Check installation.cmd".
It runs the whole process on made-up records in a temporary folder and then deletes them.
You should see:  INSTALLATION CHECK PASSED
It does not read your CSV files.


5. RUN IT (each time you want to send an update)
------------------------------------------------
  a. Make sure the trading application has finished writing its files for now.
  b. Double-click "Run NSE snapshot publisher.cmd".
  c. Wait until the window shows PUBLISH COMPLETE or PUBLISH FAILED. It usually takes under a
     minute (it waits about 5 seconds on purpose, to check that the files are not changing).


6. CHECK THAT IT WORKED
-----------------------
Success looks like this:
  - The window ends with PUBLISH COMPLETE and lists row counts for the six files.
  - A folder opens automatically, named like 20261103T121216Z-1a2b3c4d, containing two files:
        nse_snapshot_<same name>.sqlite
        current_manifest.json
  - In the output folder, LATEST_PUBLICATION.txt names this newest publication.
  - The output folder's snapshots\<same name>\validation_report.txt has the detailed checks.

If it says PUBLISH FAILED, read the message. The most common one is that a file was still being
written ("changed ... seconds ago"). Wait a minute and run again. A failure never removes or changes
an earlier publication.


7. UPLOAD TO GOOGLE DRIVE (by hand)
-----------------------------------
Mihir will share a private Google Drive folder with your Google account. Sign in to
https://drive.google.com with that account and open the shared folder.
  a. Click New > File upload. Choose the nse_snapshot_....sqlite file from the folder that opened in
     step 6. Wait until Drive says "Upload complete".
  b. Click New > File upload again and choose current_manifest.json from the same folder.
     If Drive asks about an existing file, choose "Replace existing file". Do NOT choose
     "Keep both". Two manifests would make the dashboard refuse the update.
  c. Do not rename either file, and do not upload the files from any other folder.
  d. Tell Mihir it is uploaded. If anything goes wrong, the dashboard keeps showing the previous
     snapshot and marks it STALE until a correct upload arrives.

Keep the output folder private. Its files contain your trading records. Do not email them or put
them anywhere except the shared private Drive folder.


8. STOPPING IT
--------------
Nothing keeps running after the window says PUBLISH COMPLETE or PUBLISH FAILED. Just close it.
To stop a run part-way, close the window or press Ctrl+C. An interrupted run leaves nothing half-done.
There is no background service, scheduled task or startup entry to stop.

If a later run says "Another publisher run seems to be in progress" but no other publisher window
is open, delete the file .publisher.lock in the output folder and run again.


9. REMOVING IT
--------------
  a. Delete the NSE-Snapshot-Publisher folder.
  b. Delete the output folder (for example C:\NSE-Snapshot-Publisher-output). It holds the snapshots.
  c. Optional: uninstall Python from Windows Settings > Apps, if nothing else needs it.
  d. Ask Mihir to remove your access to the shared Drive folder, or leave the folder yourself.
Nothing was installed into Windows or into your trading application, so nothing else needs undoing.
