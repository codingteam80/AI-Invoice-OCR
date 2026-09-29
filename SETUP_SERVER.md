# Pag-setup: isang Host PC, ibang PC browser lang

Isang PC (ang **Host PC**) ang nagpapatakbo ng app at may hawak ng database. Ang iba ay browser lang ang gagamitin.
Lahat ng account, invoice, at Log ay nasa iisang database sa Host PC, kaya makikita ng lahat kung sino ang nag-edit ng ano.

## Sa Host PC (isang beses lang)
1. Siguraduhing gumagana na ang app doon (`python app.py ui`).
2. Right-click `open_firewall.bat` → **Run as administrator**. Binubuksan nito ang port 8501.
3. Siguraduhing "Private" ang network profile ng Windows (Settings → Network → Properties → Private). Hindi bubukas ang firewall rule sa "Public".
4. Sa Power settings, ilagay ang **Sleep: Never** habang may gumagamit, kasi titigil ang app kapag natulog ang PC.
5. (Optional) Bigyan ang Host PC ng fixed IP sa router para hindi nagbabago ang address.

## Araw-araw
1. Sa Host PC, i-double-click ang `start_server.bat`. Ipapakita nito ang address na parang `http://192.168.1.25:8501`.
2. **Huwag isara ang window na iyon.** Kapag isinara, titigil ang app para sa lahat.
3. Sa ibang PC: buksan ang browser → i-type ang address na iyon → mag-login gamit ang sariling account.

## Anong address ang bubuksan?
- **Sa Host PC mismo:** `http://localhost:8501`
- **Sa ibang PC:** `http://<IP-ng-Host-PC>:8501` (ipinapakita ng `start_server.bat`, o `ipconfig` → IPv4 Address)
- **Huwag** gamitin ang `http://0.0.0.0:8501` na ipinapakita ng Streamlit sa console. Hindi iyon address na mabubuksan (lalabas ang `ERR_ADDRESS_INVALID`).

## Mga account
- Unang login: `admin` / `admin1234` (o ang inilagay mo sa `DEFAULT_ADMIN_PASSWORD` sa `.env`). Pipilitin kang palitan ito.
- Sa **Users** page (admin lang) gumawa ng hiwalay na account para sa bawat tao. Huwag mag-share ng account, kasi mawawala ang silbi ng log.

## Mga paalala
- **Hindi naka-encrypt (HTTP)** ang koneksyon. Ayos sa pinagkakatiwalaang office network; huwag i-expose sa internet.
- **Backup:** regular na kopyahin ang `data/` folder ng Host PC (nandoon ang `invoices.db` at mga na-upload na larawan).
- **Sabay-sabay na upload:** mabigat ang OCR sa CPU, kaya kung maraming sabay na nag-upload ay babagal para sa lahat.
- **Hindi makabukas ang ibang PC?** Tingnan kung (a) tumatakbo ang `start_server.bat`, (b) tama ang IP (`ipconfig` sa Host PC), (c) nagawa na ang `open_firewall.bat`, (d) parehong network ang dalawang PC.
