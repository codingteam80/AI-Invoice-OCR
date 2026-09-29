# Scanner input, selected logs, and report printing

This update builds on AI-Invoice-OCR_merged1.70_modByDan_fixes_login_reset_parking.zip. Login and extraction corrections are preserved.

## Update safely

Stop the app and back up your current installation. Extract this ZIP into a new folder. Keep your working .env, database, storage, and data folders: do not overwrite your live database with the database shipped in a project archive. Run the updated application using your usual environment/launcher. No new Python packages are required beyond the project's existing requirements.

## Canon LiDE 300 over USB (Windows)

1. Install the Canon LiDE 300 ScanGear driver for your Windows version from https://ph.canon/en/support/CanoScan%20LiDE%20300/model .
2. Install the free NAPS2 Windows application from https://www.naps2.com/download . NAPS2 supplies the scanning bridge; it is not bundled in this ZIP. No subscription or paid SDK is used.
3. Open NAPS2 and test a scan using the TWAIN driver. Resolve driver/device issues there first. Close NAPS2 before scanning in AI Invoice OCR.
4. Start AI Invoice OCR in your normal Windows desktop session on the PC connected to the scanner.
5. On Upload choose **Scan invoice**, select **TWAIN**, and click **Find / refresh scanners**. Select the Canon, choose paper size/color, and start with 300 DPI.
6. Put a page on the glass and click **Scan page**. Preview it; discard it and rescan if necessary. Repeat for additional pages (up to 20 per batch, within the upload size limit).
7. Choose **Separate invoices** for unrelated receipts or **One multi-page PDF** for pages belonging together. Click **Process scanned invoices**. This uses the same local storage, OCR, duplicate checks, and result display as uploaded files.

Scans awaiting processing are held in your current session and cleared by logout. A refresh may discard pending scans. Temporary scan files are removed after acquisition. Processed originals are saved through the normal upload pipeline. No automatic OCR is run by NAPS2.

The usual NAPS2 install path is detected automatically. For a portable/custom installation add the following to your existing .env and restart:

```
NAPS2_CONSOLE_PATH="C:/Program Files/NAPS2/NAPS2.Console.exe"
```

The setting must name the console executable, not NAPS2.exe. Scanning is a flatbed operation in this release; automatic document feeders/duplex are not exposed. The app does not install drivers or NAPS2 automatically.

## Office Wi-Fi/network devices

The scanner must be available to the Windows PC running the app. Try its manufacturer's TWAIN or WIA driver, or eSCL for a compatible network scanner. Connection type alone does not guarantee compatibility. Test the actual device in NAPS2 first; firewall/device access policies may apply. This integration does not control a scanner attached to a different browser PC.

If scanning times out, check the USB/network connection and close other scanning applications. If the device stays busy, restart its driver/device before retrying. Scanner discovery may take up to 45 seconds; scan attempts time out after 180 seconds.

## Filtered activity logs

Filter/search the Log page, choose XLSX, CSV, or PDF, and click **Download filtered logs**. All matching records are included; there are no selection checkboxes. With no filters/search, all logs are included. The old 2,000-record loading cap has been removed for this page. Original logs remain intact. Columns include the audit Log ID, PC-local date/time with timezone offset, user, action, type, record ID, and complete details.

To print the same filtered records, click **Prepare log print preview**, then **Print / choose printer** inside the preview. Use the existing PDF export to save a copy. The log PDF uses a landscape table with repeated column headers; long details can continue across pages. Download and preview actions are logged under the signed-in account. A new action may appear when the page next refreshes.

## Printing reports

Apply report filters and optionally generate a chart. In **Print report**, acknowledge the warning if any selected invoices are unlocked, then click **Prepare print preview**. Inside the preview, click **Print / choose printer**. Choose an installed printer or **Save as PDF**. **Download print PDF** saves the exact source PDF without browser-added headers or footers.

Printing now uses the same PDF exporter as **Generate Export → pdf**: the A3 landscape invoice table, all export columns, totals row, and optional separate chart page. The old per-invoice block layout has been removed. The browser preview renders vector pages directly from that PDF; it does not rebuild the table in HTML. Choose A3 landscape and turn off browser headers/footers for matching page layout. If your printer only supports A4, use landscape and Fit to page; the wide table will be smaller. The source PDF download retains searchable text.

The preview is invalidated when the filtered records or chart change. Existing Excel/CSV/PDF exports remain available. The log records **PRINT PREVIEW**, not successful physical printing, since browser printing cannot confirm whether the dialog was cancelled or paper was printed.

## Login after server shutdown

Browser refresh, navigation, and reconnection keep login while the same Streamlit server process is running. Explicit Sign out still clears the browser login and temporary Upload results. Stopping/restarting Streamlit, rebooting, or powering off the PC invalidates previous tokens: a new sign-in is required when the app returns. Successful sign-in opens Home.

This uses a random secret held only in the Python server process. Old tokens cannot authenticate after a restart even if the old browser cookie remains. No shutdown handler or successful cleanup on power loss is required. A page already open while the server is offline may still show its old content until it reconnects; the stopped server cannot update that browser page. Existing account passwords, invoice records, and audit history are preserved. This design targets the current single-process, single-PC deployment.

## Validation

Targeted tests cover shared PDF rendering, chart pages, long log details, scanner regression checks, and browser token validation across two separate Python processes. Streamlit UI tests use simulated account/data services and verify filtered log output, no row-selection controls, preview invalidation, and the existing unlocked-invoice warning. The supplied invoice PDF and activity-log workbook were used for visual checks. Physical printing and browser print-dialog settings still need confirmation on your PC. Scanner integration was not changed in this update.
