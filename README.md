# read-later

Send an article link from your iPhone or laptop → the article text is extracted
(trafilatura), rendered as an e-reader-sized PDF (WeasyPrint), and written to your
e-reader sync folder on the home server.

```
iPhone Share sheet ─┐
Laptop bookmarklet ─┼─► POST /save on home server ─► extract ─► PDF ─► /srv/ereader-sync/articles/
Web form            ─┘
```

## 1. Deploy on the Debian server

```bash
# copy this directory to the server, then:
cp .env.example .env
sed -i "s/change-me/$(openssl rand -hex 24)/" .env    # sets READ_LATER_TOKEN
id -u; id -g                                           # put these in docker-compose.yml `user:`
nano docker-compose.yml                                # set the sync folder path (left side of the volume)
docker compose up -d --build
curl http://localhost:8765/healthz                     # -> ok
```

Test it:

```bash
curl -X POST -H "Authorization: Bearer $(grep TOKEN .env | cut -d= -f2)" \
     --form-string url=https://paulgraham.com/greatwork.html http://localhost:8765/save
```

PDFs are named `YYYY-MM-DD Title.pdf` and written atomically, so the sync tool never
picks up a half-written file.

### Page size

The defaults are set for the **Sony DPT-RP1**. Its 13.3" screen has a 3:4 ratio, about
203×270 mm. Pages are exactly that size, so each one fills the screen at 1:1 with no
letterboxing or zooming. Text is 13pt serif with 16 mm margins, which keeps lines to a
comfortable length.

To change the layout, edit `PAGE_WIDTH`, `PAGE_HEIGHT`, `PAGE_MARGIN` and `FONT_SIZE` in
`.env`, then run `docker compose up -d`. For example, a wider right margin leaves room for
handwritten notes: `PAGE_MARGIN=16mm 45mm 16mm 16mm`.

## 2. Reaching it from the phone when you're away from home

Inside your LAN, use `http://<server-ip>:8765`. To save links from anywhere, the
easiest option is **Tailscale** on the server and the iPhone. Then use the server's
Tailscale name (e.g. `http://homeserver:8765`) everywhere below, and don't open a
port on your router. If you'd rather expose it publicly, put it behind a reverse
proxy with HTTPS (Caddy is easiest), because the token is a bearer secret.

## 3. iPhone: Share-sheet Shortcut

In the **Shortcuts** app, create a new shortcut named "Read later":

1. Tap the **ⓘ** (details) button → turn on **Show in Share Sheet**. Set the accepted
   types to **URLs** and **Safari web pages**.
2. Add the action **Get Contents of URL**:
   - URL: `http://homeserver:8765/save`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <your token>`
   - Request Body: **Form**, field `url` = **Shortcut Input**
3. Add the action **Show Notification**, with the body set to **Contents of URL**.

Now in Safari, or any app with a Share button, tap **Share → Read later**. You'll get
a notification like "Saved: 2026-11-22 Some Title", or the reason it failed.

## 4. Laptop

**Bookmarklet.** Create a bookmark with this as its URL, replacing the host and token:

```
javascript:window.open('http://homeserver:8765/save?token=YOUR_TOKEN&url='+encodeURIComponent(location.href),'_blank','width=500,height=300')
```

Click it on any article. A small window confirms the save.

**Web form.** Open `http://homeserver:8765/?token=YOUR_TOKEN` and paste a link.

**Terminal.** Use the `curl` command shown above.

## Limitations

- Only pages whose text is in the server-delivered HTML work. Paywalled articles
  and JavaScript-only sites fail with a "Could not find article text" message.
- Some sites block automated fetches. The server tries a browser user agent first,
  then an honest one.
