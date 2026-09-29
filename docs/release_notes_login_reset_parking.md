# Login, upload reset, and receipt corrections

Base: AI-Invoice-OCR_merged1.70_modByDan_fixes.zip.

## Changes

- Refresh/reconnect restores the authenticated account using a random browser token. Only its hash is stored in the new browser_login_sessions database table, created automatically on startup. Sign out revokes it and removes the cookie. Password resets and account deactivation invalidate existing tokens too.
- Explicit sign-in always opens Home. Refreshing a signed-in page keeps access to that page.
- Sign out clears temporary Upload results, pending files, and page selections. New sign-ins do not load the old shared last_upload_results.json. Saved invoice records and local invoice files are retained.
- Globe vendor address keeps the first complete printed location (Taguig) and omits the separate Cebu office. Focused extraction prompts request the first location.
- Blank customer Name/TIN labels with writing lines become null (displayed as a dash where applicable).
- Parking VAT parsing separates the printed percentage from the amount. The correction requires a printed amount corroborated by the VAT-inclusive total; it does not substitute a hardcoded receipt amount.

## Applying the update

Stop the running app before replacing application code. Keep a backup of your existing installation. Preserve your current .env, database, storage, and data folders; do not overwrite your live invoice database with a sample database from this ZIP. Start the app with its usual launcher. No additional Python dependency is required by this change. Browser cookies must be allowed for the local app. Use the same local URL consistently (localhost and 127.0.0.1 are separate cookie hosts).

These extraction changes apply when processing invoices. Existing saved invoices are not rewritten; edit their fields or reprocess them using your normal duplicate-handling workflow.

## Verification

- 60 targeted and existing regression tests passed (5 new tests plus 55 existing field/ownership/reliability tests).
- Streamlit AppTest checked sign-in, logout token revocation, Home routing, cleared Upload results, and restoring an account in a new Streamlit session. The cookie component was simulated in this test.
- The JavaScript cookie bridge passed separate write/read/clear acknowledgement checks with a simulated document.
- Replayed saved ParkingTickets1 OCR with the reported wrong fields: VAT corrected to 23.57, name and TIN cleared. ParkingTickets2 retained 25.71. The saved Globe PDF address was reduced to the first Taguig location.
- A real browser run could not be completed because this environment had no browser and the browser download failed. Full OCR/vision models were not run here. On your PC, verify sign-in as user/admin, refresh on Upload, sign out, sign in again, confirm Home, and confirm Upload has no previous result cards.
