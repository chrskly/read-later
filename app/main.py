"""read-later: save an article URL as an e-reader-friendly PDF in a sync folder."""

import hmac
import html
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura
from lxml import html as lxml_html
from flask import Flask, Response, request
from weasyprint import CSS, HTML, default_url_fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("read-later")

TOKEN = os.environ.get("READ_LATER_TOKEN", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
PAGE_WIDTH = os.environ.get("PAGE_WIDTH", "203mm")
PAGE_HEIGHT = os.environ.get("PAGE_HEIGHT", "270mm")
PAGE_MARGIN = os.environ.get("PAGE_MARGIN", "16mm")
FONT_SIZE = os.environ.get("FONT_SIZE", "13pt")
FONT_FAMILY = os.environ.get("FONT_FAMILY", "DejaVu Serif, Liberation Serif, serif")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

HONEST_USER_AGENT = "read-later/1.0 (personal read-it-later tool; python-requests)"

app = Flask(__name__)


class SaveError(Exception):
    pass


# ---------------------------------------------------------------- extraction

def fetch(url: str) -> str:
    try:
        r = None
        # Most news sites want a browser UA; some (e.g. Wikipedia) reject a fake one.
        for ua in (USER_AGENT, HONEST_USER_AGENT):
            r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "en-GB,en;q=0.9"}, timeout=30)
            if r.status_code not in (403, 429):
                break
        r.raise_for_status()
    except requests.RequestException as e:
        raise SaveError(f"Could not fetch page: {e}") from e
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


# trafilatura's "html" output still contains some of its internal XML tags.
TAG_MAP = {
    "graphic": "img", "row": "tr", "cell": "td", "list": "ul", "item": "li",
    "lb": "br", "quote": "blockquote", "code": "pre", "ref": "a", "head": "h2", "hi": "em",
}
BLOCK_TAGS = {"p", "table", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "div"}


def _px(v) -> int:
    try:
        return int(str(v).rstrip("px"))
    except (TypeError, ValueError):
        return 0


def clean_body(body: str) -> str:
    root = lxml_html.fromstring(body)

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag
        if tag in TAG_MAP:
            el.tag = TAG_MAP[tag]
            rend = (el.get("rend") or "").lower()
            if tag == "head" and rend in ("h1", "h2", "h3", "h4", "h5", "h6"):
                el.tag = "h3" if rend == "h1" else rend  # keep h1 for the document title
            elif tag == "hi" and "b" in rend:
                el.tag = "strong"
            elif tag == "list" and rend == "ol":
                el.tag = "ol"
            elif tag == "cell" and el.get("role") == "head":
                el.tag = "th"

    # Drop spacer / tracking / decorative images.
    for img in list(root.iter("img")):
        w, h = _px(img.get("width")), _px(img.get("height"))
        if not img.get("src") or (w and w < 40) or (h and h < 40) or "1x1" in img.get("src", ""):
            img.drop_tree()
        else:
            for attr in ("width", "height", "border", "hspace", "vspace", "usemap", "ismap"):
                img.attrib.pop(attr, None)

    # Unwrap layout tables (a table whose cells hold whole paragraphs / lots of text),
    # dropping cells that contain no text (e.g. nav sidebars).
    # trafilatura sometimes emits bare <tr> rows with no enclosing <table>.
    for tr in list(root.iter("tr")):
        parent = tr.getparent()
        if parent is not None and parent.tag not in ("table", "thead", "tbody", "tfoot"):
            table = lxml_html.Element("table")
            tr.addprevious(table)
            table.append(tr)

    changed = True
    while changed:
        changed = False
        for table in list(root.iter("table")):
            cells = table.xpath("./tr/td|./tr/th|./*/tr/td|./*/tr/th")
            is_layout = any(
                len(c.text_content()) > 600 or any(d.tag in BLOCK_TAGS for d in c.iter() if d is not c)
                for c in cells
            )
            if not is_layout:
                continue
            wrapper = lxml_html.Element("div")
            for c in cells:
                if c.text_content().strip():
                    c.tag = "div"
                    wrapper.append(c)
            table.addprevious(wrapper)
            table.drop_tree()
            changed = True
            break

    # Collapse runs of <br> that leave big gaps.
    for br in list(root.iter("br")):
        nxt = br.getnext()
        if nxt is not None and nxt.tag == "br" and not (br.tail or "").strip():
            br.drop_tree()

    body_el = root.find(".//body")
    target = body_el if body_el is not None else root
    return "".join(lxml_html.tostring(c, encoding="unicode") for c in target) + (target.text or "")


def extract(url: str, page_html: str) -> dict:
    body = trafilatura.extract(
        page_html,
        url=url,
        output_format="html",
        include_images=True,
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=True,
    )
    if not body or len(re.sub(r"<[^>]+>", "", body).strip()) < 200:
        raise SaveError("Could not find article text on that page (paywall or JS-only site?)")

    meta = trafilatura.extract_metadata(page_html, default_url=url)
    title = (meta.title if meta and meta.title else None) or urlparse(url).netloc
    return {
        "title": title.strip(),
        "author": meta.author if meta else None,
        "site": (meta.sitename if meta else None) or urlparse(url).netloc,
        # htmldate fills an unknown month/day with 01-01; show just the year then.
        "date": (meta.date[:4] if meta.date.endswith("-01-01") else meta.date) if meta and meta.date else None,
        "body": clean_body(body),
    }


# ---------------------------------------------------------------- rendering

def build_css() -> str:
    return f"""
    @page {{
        size: {PAGE_WIDTH} {PAGE_HEIGHT};
        margin: {PAGE_MARGIN};
        @bottom-center {{ content: counter(page) " / " counter(pages); font-size: 9pt; color: #555; }}
    }}
    html {{ font-family: {FONT_FAMILY}; font-size: {FONT_SIZE}; line-height: 1.45; color: #000; }}
    body {{ margin: 0; hyphens: auto; text-align: left; }}
    header {{ border-bottom: 0.5pt solid #000; margin-bottom: 1em; padding-bottom: 0.5em; }}
    header h1 {{ font-size: 1.5em; line-height: 1.2; margin: 0 0 0.3em; }}
    header .meta {{ font-size: 0.8em; color: #333; }}
    header .src {{ font-size: 0.7em; color: #555; word-break: break-all; }}
    h1, h2, h3, h4 {{ line-height: 1.25; page-break-after: avoid; }}
    main h1 {{ font-size: 1.3em; }} h2 {{ font-size: 1.2em; }} h3 {{ font-size: 1.1em; }}
    p {{ margin: 0 0 0.7em; orphans: 2; widows: 2; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 0.5em auto; }}
    figure {{ margin: 0.8em 0; }}
    blockquote {{ margin: 0.8em 0 0.8em 0.8em; padding-left: 0.6em; border-left: 1.5pt solid #777; }}
    pre, code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 0.8em; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; }}
    table {{ border-collapse: collapse; font-size: 0.85em; max-width: 100%; }}
    td, th {{ border: 0.5pt solid #999; padding: 2pt 4pt; }}
    a {{ color: inherit; text-decoration: none; }}
    """


def safe_url_fetcher(url, timeout=15, ssl_context=None):
    # Article HTML is untrusted: only allow remote http(s) resources (no file://, data is fine).
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https", "data"):
        raise ValueError(f"Blocked resource scheme: {scheme}")
    return default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)


def render_pdf(url: str, art: dict) -> bytes:
    meta_bits = [b for b in (art["author"], art["site"], art["date"]) if b]
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
    <title>{html.escape(art['title'])}</title>
    {f'<meta name="author" content="{html.escape(art["author"])}">' if art["author"] else ''}
    </head><body>
    <header>
      <h1>{html.escape(art['title'])}</h1>
      <div class="meta">{html.escape(' · '.join(meta_bits))}</div>
      <div class="src">{html.escape(url)}</div>
    </header>
    <main>{art['body']}</main>
    </body></html>"""
    return HTML(string=doc, base_url=url, url_fetcher=safe_url_fetcher).write_pdf(
        stylesheets=[CSS(string=build_css())]
    )


def filename_for(art: dict) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", art["title"])
    title = re.sub(r"\s+", " ", title).strip()[:90].rstrip(". ") or "article"
    return f"{date.today().isoformat()} {title}.pdf"


def save(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SaveError("That doesn't look like an http(s) URL")

    art = extract(url, fetch(url))
    pdf = render_pdf(url, art)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / filename_for(art)
    stem, n = target.stem, 2
    while target.exists():
        target = OUTPUT_DIR / f"{stem} ({n}).pdf"
        n += 1

    # Write atomically so the sync tool never picks up a half-written file.
    with tempfile.NamedTemporaryFile(dir=OUTPUT_DIR, prefix=".tmp-", suffix=".part", delete=False) as f:
        f.write(pdf)
        tmp = Path(f.name)
    os.chmod(tmp, 0o644)
    tmp.rename(target)
    log.info("Saved %s -> %s", url, target.name)
    return target


# ---------------------------------------------------------------- web

def authorised() -> bool:
    if not TOKEN:
        return False
    supplied = request.values.get("token", "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        supplied = auth[7:]
    return hmac.compare_digest(supplied.encode(), TOKEN.encode())


def extract_url(text: str) -> str:
    # Share sheets sometimes send "Title https://…" — pull out the first URL.
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else (text or "").strip()


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Read later</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; padding: 0 1rem; }}
 input {{ width: 100%; font-size: 1rem; padding: .5rem; box-sizing: border-box; margin: .3rem 0; }}
 button {{ font-size: 1rem; padding: .5rem 1rem; }}
 .ok {{ color: #186a3b; }} .err {{ color: #a93226; }}
</style></head><body>
<h1>Read later</h1>
{message}
<form method="post" action="save">
 <input name="url" type="url" placeholder="https://…" required autofocus>
 <input name="token" type="password" placeholder="token" value="{token}">
 <button>Save to e-reader</button>
</form>
</body></html>"""


@app.get("/")
def index():
    return PAGE.format(message="", token=html.escape(request.args.get("token", "")))


@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/save", methods=["GET", "POST"])
def save_route():
    wants_html = "text/html" in request.headers.get("Accept", "")
    token_val = html.escape(request.values.get("token", ""))

    def reply(msg: str, status: int):
        if wants_html:
            cls = "ok" if status == 200 else "err"
            return Response(PAGE.format(message=f'<p class="{cls}">{html.escape(msg)}</p>', token=token_val),
                            status=status, mimetype="text/html")
        return Response(msg + "\n", status=status, mimetype="text/plain")

    if not authorised():
        return reply("Unauthorised (bad or missing token)", 401)

    url = extract_url(request.values.get("url", ""))
    try:
        path = save(url)
    except SaveError as e:
        log.warning("Failed %s: %s", url, e)
        return reply(f"Failed: {e}", 422)
    except Exception as e:  # noqa: BLE001
        log.exception("Error saving %s", url)
        return reply(f"Error: {e}", 500)
    return reply(f"Saved: {path.stem}", 200)
