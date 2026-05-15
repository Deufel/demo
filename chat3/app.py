# /// script
# requires-python = ">=3.12"
# dependencies = ["py-sse>=0.3.2", "html-tags>=0.4.4"]
# ///


import base64, hashlib, hmac, os, time
from urllib.parse import parse_qs
from py_sse import serve, signals, set_cookie, html, redirect, no_content, blob, error, Changes
from py_sse.db import Database
from html_tags import h
from html_tags import render as h_render


DATASTAR = "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"
STICK = "https://cdn.jsdelivr.net/gh/Deufel/toolbox@d32d8da/css/style.css"

SESSION_MAX_AGE = 7 * 24 * 3600
UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
AGGREGATE_MAX_BYTES = 5 * UPLOAD_MAX_BYTES
EXPIRY_SECONDS = 24 * 3600
SWEEP_INTERVAL_S = 300
UPLOAD_WIRE_MAX = int(UPLOAD_MAX_BYTES * 1.4) + 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS msgs (
    id INTEGER PRIMARY KEY,
    author TEXT NOT NULL,
    txt TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS msgs_ts ON msgs(ts);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    blob BLOB NOT NULL,
    orig_name TEXT NOT NULL,
    uploader TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_ts ON files(ts);
"""

DB_PATH = os.environ.get("DB_PATH", "app.db")
db = Database(DB_PATH, schema=SCHEMA, table_topics=dict(msgs=("feed",), files=("feed",)), dev_mode=True)
_SECRET = os.urandom(32)

def _b64e(d): return base64.urlsafe_b64encode(d).rstrip(b"=").decode()
def _b64d(s): return base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode())

def sign(value, ts=None):
    ts = int(ts or time.time())
    payload = f"{_b64e(value.encode())}.{ts:x}"
    mac = _b64e(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{mac}"

def unsign(signed, max_age=SESSION_MAX_AGE):
    if not signed: return None
    parts = signed.split(".")
    if len(parts) != 3: return None
    enc, ts_hex, sig = parts
    expected = _b64e(hmac.new(_SECRET, f"{enc}.{ts_hex}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected): return None
    try:
        ts = int(ts_hex, 16)
        if time.time() - ts > max_age: return None
        return _b64d(enc).decode()
    except Exception:
        return None

def attach_user(req): req["user"] = unsign(req["cookies"].get("session", ""))

def user_hue(name): return hash(name) % 360

def fmt_size(n):
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024: return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"

def page(*body_children):
    return h.html({"data-ui-theme": "dark"},
        h.head(h.title("py_sse chat"), h.meta(charset="utf-8"), h.meta(name="viewport", content="width=device-width, initial-scale=1"),
               h.link(rel="stylesheet", href=STICK), h.script(type="module", src=DATASTAR)),
        h.body({"class": "page stage"}, *body_children))

def login_page():
    return page(
        h.main({"class": "pg-main"},
            h.div({"class": "hud-overlay"},
                h.div({"class": "Card stage •", "style": "min-inline-size: min(22rem, 90vw)"},
                    h.div({"class": "column"},
                        h.div(h.div({"style": "--type: 2; font-weight: 600"}, "py_sse chat"),
                               h.div({"style": "--type: -1; --fg: -0.5"}, "pick a name and start typing")),
                        h.form({"method": "post", "action": "/login"},
                            h.div({"class": "column"},
                                h.div(h.label({"for": "username"}, "display name"),
                                      h.input({"class": "input", "id": "username", "name": "username", "autofocus": True, "maxlength": "32", "required": True})),
                                h.button({"class": "btn", "type": "submit", "style": "--bg: 0.8"}, "sign in")
                                  )
                               )
                          )
                      )
                  )
               )
    )

def chat_page(user):
    return page(
        h.div({"class": "pg-header"},
            h.div({"class": "spread"},
                h.div({"class": "row"},
                    h.span({"style": "--type: 1; font-weight: 700"}, "py_sse chat"),
                    h.span({"class": "tag suc"}, "live")),
                h.div({"class": "row"},
                    h.span({"class": "avatar", "style": f"--hue-lock: {user_hue(user)}"}, user[:2].upper()),
                    h.span({"class": "desktop", "style": "--fg: -0.5"}, user),
                    h.form({"method": "post", "action": "/logout"},
                        h.button({"class": "btn", "type": "submit"}, "sign out"))))),
        h.main({"class": "pg-main"},
            h.div({"id": "feed", "data-init": "@get('/chat/feed')"})),
        h.div({"class": "pg-main-footer"},
            h.style("@scope {:scope > div.composer {display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: calc(0.25 * 1lh);}:scope > div.attached {margin-block-start: calc(0.25 * 1lh);}}"),
            h.div({"class": "composer"},
                h.label({"class": "icon-btn", "title": "attach file"},
                    h.span({"data-show": "!$files.length", "aria-hidden": "true"}, "📎"),
                    h.span({"data-show": "$files.length", "aria-hidden": "true"}, "📄"),
                    h.input({"type": "file", "class": "vh", "data-bind:files": True})),
                h.input({"class": "input", "data-bind:text": True, "placeholder": "say something  (or attach a file)", "autofocus": True, "maxlength": "500",
                    "data-on:keydown": "evt.key === 'Enter' && !evt.shiftKey && ($text.trim() || $files.length) && (evt.preventDefault(), @post('/chat/say'), $text='', $files=[], $filesMimes=[], $filesNames=[])" }),
                h.button({"class": "btn", "type": "button", "data-on:click": "($text.trim() || $files.length) && (@post('/chat/say'), $text='', $files=[], $filesMimes=[], $filesNames=[])", "data-attr:disabled": "!$text.trim() && !$files.length", "style": "--bg: 0.8"}, "send")),
            h.div({"data-show": "$files.length", "class": "row attached"},
                h.span({"class": "tag inf", "data-text": "$filesNames?.[0]"}),
                h.span({"style": "--type: -2; --fg: -0.5"}, "attached — × to remove"),
                h.button({"class": "icon-btn", "data-on:click": "$files=[]; $filesMimes=[]; $filesNames=[]", "title": "remove"}, "×"))),
        h.div({"class": "pg-footer", "style": "--type: -2; --fg: -0.5; text-align: center"}, f"messages & files auto-expire after 24h · max {fmt_size(UPLOAD_MAX_BYTES)} per file"),
        h.div({"class": "vh", "data-signals": "{me: " + repr(user) + ", text: '', files: [], filesMimes: [], filesNames: []}"}))

def render_item_msg(item_id, author, txt):
    return h.div({"id": f"msg-{item_id}", "class": "card stage column", "style": f"--hue-lock: {user_hue(author)}"},
        h.div({"class": "spread"},
            h.div({"class": "row"},
                h.span({"class": "avatar", "style": "--bg: 0.4; --type: -2"}, author[:2].upper()),
                h.span({"style": "font-weight: 600"}, author)),
            h.button({"class": "btn dgr", "data-on:click": f"@post('/chat/del/{item_id}')", "data-show": f"$me === {repr(author)}", "style": "--type: -2"}, "delete")),
        h.div({"style": "white-space: pre-wrap; word-break: break-word"}, txt))

def render_item_file(item_id, uploader, orig_name, size):
    return h.div({"id": f"file-{item_id}", "class": "card stage column", "style": f"--hue-lock: {user_hue(uploader)}"},
        h.div({"class": "spread"},
            h.div({"class": "row"},
                h.span({"class": "avatar", "style": "--bg: 0.4; --type: -2"}, uploader[:2].upper()),
                h.span({"style": "font-weight: 600"}, uploader),
                h.span({"class": "tag inf"}, "file")),
            h.button({"class": "btn dgr", "data-on:click": f"@post('/chat/files/del/{item_id}')", "data-show": f"$me === {repr(uploader)}", "style": "--type: -2"}, "delete")),
        h.div({"class": "flank-end card"},
            h.a({"href": f"/files/{item_id}", "download": orig_name, "class": "link truncate"}, orig_name),
            h.span({"class": "nowrap", "style": "--type: -2; --fg: -0.5"}, fmt_size(size))))

def render_feed():
    rows = db.all("SELECT kind, id, who, payload, size, ts FROM (SELECT 'm' AS kind, id, author AS who, txt AS payload, 0 AS size, ts FROM msgs UNION ALL SELECT 'f' AS kind, id, uploader, orig_name, size, ts FROM files) ORDER BY ts ASC LIMIT 200")
    if not rows: return h.div({"id": "feed", "class": "column"}, h.div({"class": "card", "style": "text-align: center; --fg: -0.5"}, "no messages or files yet — say hi or attach."))
    children = []
    for kind, item_id, who, payload, size, _ts in rows:
        children.append(render_item_msg(item_id, who, payload) if kind == "m" else render_item_file(item_id, who, payload, size))
    return h.div({"id": "feed", "class": "column"}, *children)

def _decode_b64(s):
    if not isinstance(s, str): return None
    if s.startswith("data:"): _, _, s = s.partition(",")
    try: return base64.b64decode(s, validate=False)
    except Exception: return None

def extract_files(sig):
    if not isinstance(sig, dict): return
    files = sig.get("files")
    if not isinstance(files, list) or not files: return
    first = files[0]
    if isinstance(first, str):
        mimes, names = sig.get("filesMimes") or [], sig.get("filesNames") or []
        for i, b64 in enumerate(files):
            raw = _decode_b64(b64)
            if raw: yield raw, str(names[i] if i < len(names) else "file")[:200] or "file", str(mimes[i] if i < len(mimes) else "application/octet-stream")[:100]
    elif isinstance(first, dict):
        for obj in files:
            if isinstance(obj, dict):
                data = obj.get("contents") or obj.get("dataURL") or obj.get("data")
                raw = _decode_b64(data) if isinstance(data, str) else None
                if raw: yield raw, str(obj.get("name") or "file")[:200] or "file", str(obj.get("mime") or obj.get("type") or "application/octet-stream")[:100]

def get_root(req): return redirect("/chat" if req["user"] else "/login")
def get_login(req): return redirect("/chat") if req["user"] else html(h_render(login_page()))
def post_login(req):
    form = {k: v[0] for k, v in parse_qs(req["body"].decode("utf-8", "replace")).items()}
    name = (form.get("username") or "").strip()[:32]
    if not name: return redirect("/login")
    set_cookie(req, "session", sign(name), max_age=SESSION_MAX_AGE, path="/", httponly=True, samesite="Lax")
    return redirect("/chat")
def post_logout(req):
    set_cookie(req, "session", "", max_age=0, path="/", httponly=True, samesite="Lax")
    return redirect("/login")
def get_chat(req): return redirect("/login") if not req["user"] else html(h_render(chat_page(req["user"])))

def post_say(req):
    if not req["user"]: return error(401, "auth required")
    if len(req["body"]) > UPLOAD_WIRE_MAX: return error(413, "payload too large")
    sig = signals(req)
    txt = (sig.get("text") or "").strip()[:500] if isinstance(sig, dict) else ""
    if txt: db.execute("INSERT INTO msgs(author, txt, ts) VALUES(?, ?, ?)", (req["user"], txt, time.time()))
    items = list(extract_files(sig))
    if items:
        current = db.one("SELECT COALESCE(SUM(size), 0) FROM files")[0]
        for raw, name, mime in items:
            if len(raw) > UPLOAD_MAX_BYTES: continue
            if current + len(raw) > AGGREGATE_MAX_BYTES: return error(507, "storage full")
            current += len(raw)
            db.execute("INSERT INTO files(blob, orig_name, uploader, mime, size, ts) VALUES(?, ?, ?, ?, ?, ?)", (raw, name, req["user"], mime, len(raw), time.time()))
    return no_content()

def post_delete_msg(req):
    if not req["user"]: return error(401)
    db.execute("DELETE FROM msgs WHERE id = ? AND author = ?", (int(req["params"]["msg_id"]), req["user"]))
    return no_content()

def post_delete_file(req):
    if not req["user"]: return error(401)
    db.execute("DELETE FROM files WHERE id = ? AND uploader = ?", (int(req["params"]["file_id"]), req["user"]))
    return no_content()

def sse_event_patch(html_str): return f"event: datastar-patch-elements\ndata: elements {html_str}"

def get_feed(req):
    if not req["user"]:
        yield sse_event_patch("<div id='feed'></div>")
        return
    yield sse_event_patch(h_render(render_feed()))
    while True:
        db.changes.wait(timeout=10)
        try: yield sse_event_patch(h_render(render_feed()))
        except (OSError, BrokenPipeError): return

def get_file(req):
    if not req["user"]: return redirect("/login")
    try: file_id = int(req["params"]["file_id"])
    except Exception: return error(404, "not found")
    row = db.one("SELECT blob, orig_name, mime FROM files WHERE id = ?", (file_id,))
    if row is None: return error(404, "not found")
    body, orig_name, mime = row
    return blob(body, mime, filename=orig_name)

ROUTES = [
    ("GET", "/", get_root),
    ("GET", "/login", get_login),
    ("POST", "/login", post_login),
    ("POST", "/logout", post_logout),
    ("GET", "/chat", get_chat),
    ("GET", "/chat/feed", get_feed),
    ("POST", "/chat/say", post_say),
    ("POST", "/chat/del/{msg_id}", post_delete_msg),
    ("POST", "/chat/files/del/{file_id}", post_delete_file),
    ("GET", "/files/{file_id}", get_file),
]

if __name__ == "__main__":
    serve(ROUTES, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), before_hooks=[attach_user])

