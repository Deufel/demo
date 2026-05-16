# /// script
# requires-python = ">=3.12"
# dependencies = ["py-sse>=0.5.0", "html-tags>=0.4.4"]
# ///
"""
Golf scorecard — CQRS demo on py_sse + html_tags + Datastar.

Identity flow
-------------
GET  /           passcode form
POST /login      verify code, set auth cookie, redirect to /me
GET  /me         pick existing user or create new
POST /me         set uid cookie, redirect to /games
GET  /logout     clear cookies, back to /

Game flow
---------
GET  /games                                       list (read stream)
POST /games                                       create
GET  /games/stream                                SSE: re-render list
POST /games/{id}/join                             add me to game
POST /games/{id}/guests                           add guest player
GET  /games/{id}                                  scorecard (read stream)
GET  /games/{id}/stream                           SSE: re-render scorecard
POST /games/{id}/score/{pid}/{hole}/{v}           commit a cell (0=clear)
"""

import os
import time
from py_sse import (
    serve, html, redirect, error, Database, LiveCounter,
    set_cookie,
)
from html_tags import h, Safe
from html_tags import render as h_render
from urllib.parse import parse_qs


STICK    = "https://cdn.jsdelivr.net/gh/Deufel/toolbox@d32d8da/css/style.css"
DATASTAR = "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"
PASSCODE = "1234"
NUM_HOLES = 18

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS game (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  course TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS player (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id INTEGER NOT NULL,
  user_id INTEGER,
  name TEXT NOT NULL,
  slot INTEGER NOT NULL,
  UNIQUE(game_id, slot)
);
CREATE TABLE IF NOT EXISTS score (
  game_id INTEGER NOT NULL,
  player_id INTEGER NOT NULL,
  hole INTEGER NOT NULL,
  strokes INTEGER,
  PRIMARY KEY (game_id, player_id, hole)
);
"""

db = Database("scorecard.db", schema=SCHEMA, dev_mode=False)
live = LiveCounter(soft_cap=3, min_poll_ms=1_000, max_poll_ms=8_000, ramp_users=10)


# ─── data access ──────────────────────────────────────────────────────
# All writes go through db.execute, which calls changes.notify() after
# the commit lands. Readers parked in db.changes.wait() get woken on
# any state change. Reads also notify (per framework comment: wasteful
# but harmless).

def list_users():
    return db.all("SELECT id, name FROM user ORDER BY name")

def get_user(user_id):
    return db.one("SELECT id, name FROM user WHERE id = ?", (user_id,))

def get_user_by_name(name):
    return db.one("SELECT id, name FROM user WHERE name = ?", (name,))

def create_user(name):
    db.execute("INSERT INTO user (name, created_at) VALUES (?, ?)",
               (name, int(time.time())))
    return db.one("SELECT last_insert_rowid()")[0]

def list_games():
    return db.all("SELECT id, name, course FROM game ORDER BY id DESC")

def get_game(game_id):
    return db.one("SELECT id, name, course FROM game WHERE id = ?", (game_id,))

def list_players(game_id):
    return db.all(
        "SELECT id, user_id, name, slot FROM player "
        "WHERE game_id = ? ORDER BY slot", (game_id,))

def get_my_player(game_id, user_id):
    if not user_id:
        return None
    return db.one(
        "SELECT id, slot FROM player WHERE game_id = ? AND user_id = ?",
        (game_id, user_id))

def get_scores(game_id):
    rows = db.all(
        "SELECT player_id, hole, strokes FROM score WHERE game_id = ?",
        (game_id,))
    return {(pid, hole): strokes for pid, hole, strokes in rows}

def create_game(name, course, host_user_id, host_name):
    db.execute(
        "INSERT INTO game (name, course, created_at) VALUES (?, ?, ?)",
        (name, course or "", int(time.time())))
    game_id = db.one("SELECT last_insert_rowid()")[0]
    db.execute(
        "INSERT INTO player (game_id, user_id, name, slot) VALUES (?, ?, ?, 1)",
        (game_id, host_user_id, host_name))
    return game_id

def next_slot(game_id):
    used = {row[0] for row in db.all(
        "SELECT slot FROM player WHERE game_id = ?", (game_id,))}
    return next((s for s in range(1, 5) if s not in used), None)

def join_game(game_id, user_id, name):
    existing = db.one(
        "SELECT id FROM player WHERE game_id = ? AND user_id = ?",
        (game_id, user_id))
    if existing:
        return existing[0]
    slot = next_slot(game_id)
    if slot is None:
        return None
    db.execute(
        "INSERT INTO player (game_id, user_id, name, slot) VALUES (?, ?, ?, ?)",
        (game_id, user_id, name, slot))
    return db.one("SELECT last_insert_rowid()")[0]

def add_guest(game_id, name):
    slot = next_slot(game_id)
    if slot is None:
        return None
    db.execute(
        "INSERT INTO player (game_id, user_id, name, slot) VALUES (?, NULL, ?, ?)",
        (game_id, name, slot))
    return db.one("SELECT last_insert_rowid()")[0]

def get_player_owner(player_id):
    return db.one("SELECT user_id, game_id FROM player WHERE id = ?",
                  (player_id,))

def set_score(game_id, player_id, hole, strokes):
    db.execute(
        "INSERT INTO score (game_id, player_id, hole, strokes) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (game_id, player_id, hole) "
        "DO UPDATE SET strokes = excluded.strokes",
        (game_id, player_id, hole, strokes))


# ─── auth / identity ──────────────────────────────────────────────────

def is_authed(req):
    return req["cookies"].get("auth") == "ok"

def current_user_id(req):
    raw = req["cookies"].get("uid")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# ─── before_hooks ─────────────────────────────────────────────────────
# Framework calls these before every handler. Each may mutate req.
# Set req["_redirect"] to short-circuit; handlers check via short_circuit().

# Routes that don't require auth at all.
PUBLIC_PATHS = {"/", "/login", "/healthz"}

# Routes that require auth but not yet a chosen user.
NO_USER_PATHS = PUBLIC_PATHS | {"/me", "/logout"}

def gate(req):
    """Single before_hook. Handles auth + identity gating in order:
      - public path: pass
      - no auth: → /
      - authed, no user, on a no-user path: pass
      - authed, no user, anywhere else: → /me
      - authed with user: pass
    """
    path = req["path"]
    if path in PUBLIC_PATHS:
        return
    if not is_authed(req):
        req["_redirect"] = "/"
        return
    if current_user_id(req) is None and path not in NO_USER_PATHS:
        req["_redirect"] = "/me"
        return

def short_circuit(req):
    if req.get("_redirect"):
        return redirect(req["_redirect"])
    return None


# ─── icons ───────────────────────────────────────────────────────────

def _digit_icon(path_d):
    return Safe(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" '
        'viewBox="0 0 24 24">'
        '<path d="M0 0h24v24H0z" fill="none"/>'
        f'{path_d}'
        '</svg>')

ICON_1 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" d="M13 20V4L8 9"/>')
ICON_2 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" '
                     'd="M8 8a4 4 0 1 1 8 0c0 1.098-.564 2.025-1.159 2.815L8 20h8"/>')
ICON_3 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" '
                     'd="M12 12a4 4 0 1 0-4-4m0 8a4 4 0 1 0 4-4"/>')
ICON_4 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" d="M15 20V5L7 16h10"/>')
ICON_5 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" '
                     'd="M8 20h4a4 4 0 1 0 0-8H8V4h8"/>')
ICON_6 = _digit_icon('<g fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2">'
                     '<path d="M8 16a4 4 0 1 0 8 0v-1a4 4 0 1 0-8 0"/>'
                     '<path d="M16 8a4 4 0 1 0-8 0v8"/></g>')
ICON_7 = _digit_icon('<path fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2" d="M8 4h8l-4 16"/>')
ICON_8 = _digit_icon('<g fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2">'
                     '<path d="M8 8a4 4 0 1 0 8 0a4 4 0 1 0-8 0"/>'
                     '<path d="M8 16a4 4 0 1 0 8 0a4 4 0 1 0-8 0"/></g>')
ICON_9 = _digit_icon('<g fill="none" stroke="currentColor" stroke-linecap="round" '
                     'stroke-linejoin="round" stroke-width="2">'
                     '<path d="M16 8a4 4 0 1 0-8 0v1a4 4 0 1 0 8 0"/>'
                     '<path d="M8 16a4 4 0 1 0 8 0V8"/></g>')

DIGIT_ICONS = [ICON_1, ICON_2, ICON_3, ICON_4, ICON_5,
               ICON_6, ICON_7, ICON_8, ICON_9]

ICON_X = Safe(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>')

# Golf cart icon for the brand mark — replaces the emoji.
ICON_LOGO = Safe(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" '
    'viewBox="0 0 15 15">'
    '<path d="M0 0h15v15H0z" fill="none"/>'
    '<path fill="currentColor" d="M2.63 13.5c-.92 0-1.65-.74-1.65-1.65s.73-1.65 '
    '1.65-1.65c.91 0 1.65.74 1.65 1.65s-.74 1.65-1.65 1.65m8.25 0c-.92 '
    '0-1.65-.74-1.65-1.65s.73-1.65 1.65-1.65c.91 0 1.65.74 1.65 1.65s-.74 '
    '1.65-1.65 1.65"/>'
    '<path fill="currentColor" d="M12.53 2v4.5h1.05c.55 0 .92 3.95.92 4.5s-.45.5'
    '-1 .5s-.97-1.85-2.62-1.85S8.78 12 8 12H5.5c-.55 0-1.22-2.35-2.87-2.35c-1.1 '
    '0-1.1.55-1.65.55S.5 9.55.5 9s.45-1.5 1-1.5h1L4 3H3c-.28 0-.5-.22-.5-.5S2.72 '
    '2 3 2zM11.5 3H5L3.5 7.5h1l.94-.72l-.31-.37c-.09-.11-.09-.24.04-.35c.12-.12'
    '.29-.05.35.03s.94 1.17.94 1.17c.09.11.07.26-.03.35c-.11.09-.27.07-.35-.04l'
    '-.36-.44l-.8.57c.57.26.85 1.49 1.08 2.3h2l1-1s.57-2.5 1.58-2.5h.92z"/>'
    '</svg>')


# ─── shared rendering ─────────────────────────────────────────────────

def full_page(title, *body_children, body_extra=None):
    """Single page-builder. Initial GETs and every SSE fat-morph frame
    call this. Identical head → idiomorph leaves head alone → Datastar
    script stays alive across morphs."""
    body_attrs = {"class": "page stage"}
    if body_extra:
        body_attrs.update(body_extra)
    return h.html({"id": "page", "data-ui-theme": "dark"},
        h.head(
            h.title(title),
            h.meta(charset="utf-8"),
            h.meta(name="viewport", content="width=device-width, initial-scale=1"),
            h.link(rel="stylesheet", href=STICK),
            h.script(type="module", src=DATASTAR)),
        h.body(body_attrs, *body_children))


def header_bar(req, crumbs):
    uid = current_user_id(req)
    user = get_user(uid) if uid else None
    user_name = user[1] if user else None
    parts = []
    for label, href in crumbs:
        if href:
            parts.append(h.a({"class": "link", "href": href}, label))
        else:
            parts.append(h.span(label))
    return h.header({"class": "pg-header spread"},
        h.div({"class": "row"},
            h.span({"style": "--type: 1; display: inline-flex; "
                             "align-items: center; gap: 0.3em"},
                   ICON_LOGO, h.strong("scorecard")),
            h.nav({"class": "crumbs row", "style": "--type: -1; --fg: -0.5"},
                *parts)),
        h.div({"class": "row", "style": "--type: -2; --fg: -0.5"},
            (h.span({"class": "row"},
                    h.span(f"signed in as {user_name}"),
                    h.a({"class": "link", "href": "/logout",
                         "style": "margin-inline-start: 0.5em"}, "log out"))
             if user_name else h.span("")),
            h.span({"style": "margin-inline-start: 0.5em"},
                   "py_sse + stick.css + datastar")))


# ─── score picker popover ────────────────────────────────────────────

def score_picker(game_id):
    def digit_btn(n):
        return h.button({
            "class": "icon-btn stage",
            "type": "button",
            "style": "--type: 2; aspect-ratio: 1; inline-size: 4em",
            "aria-label": str(n),
            "data-on:click":
                "evt.stopPropagation(), "
                "(()=>{const [p,hh]=$_pick.split('-'); "
                f"__action('post',evt,`/games/{game_id}/score/${{p}}/${{hh}}/{n}`)}})(), "
                "document.getElementById('picker').hidePopover()",
        }, DIGIT_ICONS[n - 1])

    clear_btn = h.button({
        "class": "icon-btn stage dgr",
        "type": "button",
        "style": "--type: 2; aspect-ratio: 1; inline-size: 4em",
        "aria-label": "clear",
        "data-on:click":
            "evt.stopPropagation(), "
            "(()=>{const [p,hh]=$_pick.split('-'); "
            f"__action('post',evt,`/games/{game_id}/score/${{p}}/${{hh}}/0`)}})(), "
            "document.getElementById('picker').hidePopover()",
    }, ICON_X)

    close_btn = h.button({
        "class": "icon-btn stage",
        "type": "button",
        "popovertarget": "picker",
        "popovertargetaction": "hide",
        "aria-label": "close",
        "style": "--type: 1",
    }, ICON_X)

    return h.dialog({
            "id": "picker",
            "popover": "auto",
            "class": "stage glass",
            "style": (
                "inset: 0; margin: auto; "
                "border: 1px solid var(--Border); "
                "border-radius: var(--cfg-radius); "
                "padding: 1lh; "
                "min-inline-size: min(20rem, 90vw); "
                "background: transparent"
            ),
        },
        h.div({"class": "column", "style": "gap: 1lh"},
            h.div({"class": "spread"},
                h.span({"style": "--type: 0; --fg: -0.5"}, "pick strokes"),
                close_btn),
            h.div({"class": "grid",
                   "style": "--grid-min: 4em; gap: 0.5lh; justify-items: center"},
                *[digit_btn(n) for n in range(1, 10)]),
            h.div({"class": "row", "style": "justify-content: center"},
                clear_btn,
                h.span({"style": "--type: -1; --fg: -0.5; align-self: center"},
                       "clear"))))


# ─── route: GET / (passcode) ──────────────────────────────────────────

def get_root(req):
    # If already authed, skip past based on whether user is chosen.
    if is_authed(req):
        if current_user_id(req):
            return redirect("/games")
        return redirect("/me")

    return html(h_render(full_page(
        "scorecard — sign in",
        header_bar(req, [("sign in", None)]),
        h.main({"class": "pg-main column",
                "style": "max-inline-size: 24rem; margin: 2lh auto"},
            h.div({"class": "card stage column"},
                h.h2({"style": "--type: 2"}, "welcome"),
                h.p({"style": "--fg: -0.5"},
                    "this demo is gated by a 4-digit code. ",
                    h.span({"class": "tag inf"}, "1234")),
                h.form({"method": "post", "action": "/login", "class": "column"},
                    h.input({"class": "input", "name": "passcode",
                             "type": "password", "inputmode": "numeric",
                             "placeholder": "••••", "autofocus": True,
                             "autocomplete": "off", "required": True}),
                    h.button({"class": "btn", "type": "submit",
                              "style": "--bg: var(--cfg-bg-loud); --fg: -1; "
                                       "border-color: transparent"},
                             "continue")))))))


def post_login(req):
    fields = parse_qs(req["body"].decode("utf-8"))
    code = (fields.get("passcode") or [""])[0]
    if code != PASSCODE:
        return redirect("/")
    set_cookie(req, "auth", "ok", path="/", max_age=86400 * 30, samesite="Lax")
    return redirect("/me")


# ─── route: GET /me (pick or create user) ─────────────────────────────

def get_me(req):
    sc = short_circuit(req)
    if sc: return sc
    users = list_users()

    return html(h_render(full_page(
        "scorecard — who are you?",
        header_bar(req, [("who are you?", None)]),
        h.main({"class": "pg-main column",
                "style": "max-inline-size: 28rem; margin: 2lh auto"},

            # Existing users
            (h.div({"class": "card stage column"},
                h.h2({"style": "--type: 2"}, "sign in as"),
                h.p({"style": "--fg: -0.5"},
                    "pick yourself if you've played here before"),
                h.div({"class": "column"},
                    *[h.form({"method": "post", "action": "/me",
                              "style": "margin: 0"},
                        h.input({"type": "hidden", "name": "user_id",
                                 "value": str(uid)}),
                        h.button({
                            "class": "btn row spread",
                            "type": "submit",
                            "style": "inline-size: 100%"},
                            h.strong(uname),
                            h.span({"class": "tag suc"}, "use")))
                      for uid, uname in users]))
             if users else h.div()),

            # Create new
            h.div({"class": "card stage column"},
                h.h2({"style": "--type: 2"},
                     "new player" if users else "what's your name?"),
                h.form({"method": "post", "action": "/me", "class": "column"},
                    h.input({"class": "input", "name": "name",
                             "placeholder": "your name",
                             "autofocus": not bool(users),
                             "required": True}),
                    h.button({"class": "btn", "type": "submit",
                              "style": "--bg: var(--cfg-bg-loud); --fg: -1; "
                                       "border-color: transparent"},
                             "create & continue")))))))


def post_me(req):
    sc = short_circuit(req)
    if sc: return sc
    fields = parse_qs(req["body"].decode("utf-8"))

    # Two modes: pick an existing user_id, or create a new one by name.
    existing_id = (fields.get("user_id") or [""])[0].strip()
    new_name = (fields.get("name") or [""])[0].strip()

    if existing_id:
        try:
            uid = int(existing_id)
        except ValueError:
            return redirect("/me")
        if not get_user(uid):
            return redirect("/me")
    elif new_name:
        # Reuse if the name already exists (avoid duplicate row).
        existing = get_user_by_name(new_name)
        uid = existing[0] if existing else create_user(new_name)
    else:
        return redirect("/me")

    set_cookie(req, "uid", str(uid), path="/", max_age=86400 * 30, samesite="Lax")
    return redirect("/games")


# ─── route: GET /logout ──────────────────────────────────────────────

def get_logout(req):
    # Clear by setting cookies with max_age=0.
    set_cookie(req, "auth", "", path="/", max_age=0)
    set_cookie(req, "uid", "", path="/", max_age=0)
    return redirect("/")


# ─── route: GET /games & stream ──────────────────────────────────────

def render_games_list(req):
    uid = current_user_id(req)
    games = list_games()
    my_games = set()
    if uid:
        rows = db.all(
            "SELECT DISTINCT game_id FROM player WHERE user_id = ?", (uid,))
        my_games = {row[0] for row in rows}

    return h.div({"class": "column"},
        h.div({"class": "card stage column"},
            h.h2({"style": "--type: 2"}, "new game"),
            h.form({"method": "post", "action": "/games", "class": "column"},
                h.fieldset(
                    h.div(h.label("game name"),
                          h.input({"class": "input", "name": "name",
                                   "placeholder": "saturday morning",
                                   "required": True})),
                    h.div(h.label("course"),
                          h.input({"class": "input", "name": "course",
                                   "placeholder": "torrey pines south"}))),
                h.button({"class": "btn", "type": "submit",
                          "style": "--bg: var(--cfg-bg-loud); --fg: -1; "
                                   "border-color: transparent"},
                         "start game"))),
        h.div({"class": "card stage column"},
            h.h2({"style": "--type: 2"}, f"games ({len(games)})"),
            (h.p({"style": "--fg: -0.5"}, "no games yet — start one above")
             if not games else
             h.div({"class": "column"},
                *[
                    h.div({"class": "card row spread",
                           "style": "padding: 0.5lh"},
                        h.a({"href": f"/games/{gid}",
                             "style": "text-decoration: none; color: inherit; "
                                      "flex: 1"},
                            h.div({"class": "column"},
                                h.strong(name),
                                h.span({"style": "--type: -1; --fg: -0.5"},
                                       course or "—"))),
                        (h.a({"class": "tag suc", "href": f"/games/{gid}",
                              "style": "text-decoration: none"}, "open")
                         if gid in my_games else
                         h.form({"method": "post",
                                 "action": f"/games/{gid}/join",
                                 "style": "margin: 0"},
                            h.button({"class": "tag inf", "type": "submit"},
                                     "join"))))
                    for gid, name, course in games]))))


def get_games(req):
    sc = short_circuit(req)
    if sc: return sc
    return html(h_render(full_page(
        "scorecard — games",
        header_bar(req, [("games", None)]),
        h.main({"class": "pg-main column",
                "style": "max-inline-size: 40rem; margin: 1lh auto",
                "data-init": "@get('/games/stream')"},
            render_games_list(req)))))


def get_games_stream(req):
    sc = short_circuit(req)
    if sc: return sc
    resource = "games-list"

    def render_frame(count, mode, interval_ms=None):
        main_attrs = {"class": "pg-main column",
                      "style": "max-inline-size: 40rem; margin: 1lh auto"}
        if mode == "poll":
            main_attrs[f"data-on-interval__duration.{interval_ms}ms"] = (
                "@get('/games/stream')")
        doc = full_page(
            "scorecard — games",
            header_bar(req, [("games", None), (f"{mode} · {count}", None)]),
            h.main(main_attrs, render_games_list(req)))
        return f"event: datastar-patch-elements\ndata: elements {h_render(doc)}"

    if not live.should_be_live(resource):
        count = live.count(resource)
        interval = live.poll_interval_ms(resource)
        yield render_frame(count, "poll", interval)
        return

    with live.join(resource):
        yield render_frame(live.count(resource), "live")
        while True:
            db.changes.wait(timeout=15)
            try:
                yield render_frame(live.count(resource), "live")
            except (OSError, BrokenPipeError):
                return


def post_games(req):
    sc = short_circuit(req)
    if sc: return sc
    fields = parse_qs(req["body"].decode("utf-8"))
    name = (fields.get("name") or [""])[0].strip()
    course = (fields.get("course") or [""])[0].strip()
    if not name:
        return redirect("/games")
    uid = current_user_id(req)
    user = get_user(uid)
    if not user:
        return redirect("/me")
    game_id = create_game(name, course, uid, user[1])
    return redirect(f"/games/{game_id}")


def post_join(req):
    sc = short_circuit(req)
    if sc: return sc
    game_id = int(req["params"]["id"])
    if not get_game(game_id):
        return error(404, "game not found")
    uid = current_user_id(req)
    user = get_user(uid)
    if not user:
        return redirect("/me")
    join_game(game_id, uid, user[1])
    return redirect(f"/games/{game_id}")


def post_guest(req):
    sc = short_circuit(req)
    if sc: return sc
    game_id = int(req["params"]["id"])
    if not get_game(game_id):
        return error(404, "game not found")
    uid = current_user_id(req)
    my = get_my_player(game_id, uid)
    if not my:
        return error(403, "not a player in this game")
    fields = parse_qs(req["body"].decode("utf-8"))
    name = (fields.get("name") or [""])[0].strip()
    if not name:
        return redirect(f"/games/{game_id}")
    add_guest(game_id, name)
    return redirect(f"/games/{game_id}")


# ─── scorecard render ────────────────────────────────────────────────

def render_scorecard(req, game_id):
    uid = current_user_id(req)
    game = get_game(game_id)
    if not game:
        return h.div("game not found")
    _, name, course = game
    players = list_players(game_id)
    scores = get_scores(game_id)
    my = get_my_player(game_id, uid)

    def can_edit(player_user_id):
        return player_user_id == uid or player_user_id is None

    def total_for(pid):
        return sum(scores.get((pid, hole), 0) or 0
                   for hole in range(1, NUM_HOLES + 1))

    header_cells = [h.th({"style": "text-align: start"}, "hole")] + [
        h.th({"class": "r"},
            h.div({"class": "column", "style": "align-items: end"},
                h.span(pname),
                h.span({"style": "--type: -2; --fg: -0.5"},
                       "guest" if puid is None
                       else ("you" if puid == uid else f"seat {slot}"))))
        for _, puid, pname, slot in players
    ]

    body_rows = []
    for hole in range(1, NUM_HOLES + 1):
        cells = [h.td({"style": "--fg: -0.5"}, str(hole))]
        for pid, puid, _pname, _slot in players:
            strokes = scores.get((pid, hole))
            txt = str(strokes) if strokes is not None else "—"
            editable = can_edit(puid)
            if editable:
                cells.append(h.td(
                    {"class": "r",
                     "style": "cursor: pointer; --bg: 0.1",
                     "data-on:click":
                         f"$_pick = '{pid}-{hole}', "
                         "document.getElementById('picker').showPopover()"},
                    h.span(txt)))
            else:
                cells.append(h.td({"class": "r", "style": "--fg: -0.5"}, txt))
        body_rows.append(h.tr(*cells))

    total_cells = [h.td({"style": "--fg: -0.5"}, "total")] + [
        h.td({"class": "r"}, h.strong(str(total_for(pid))))
        for pid, _, _, _ in players
    ]
    body_rows.append(h.tr(
        {"style": "border-block-start: 2px solid var(--Border)"},
        *total_cells))

    can_add_guest = my and (len(players) < 4)
    open_seats = 4 - len(players)
    is_in_game = my is not None

    return h.div({"class": "column"},
        h.div({"class": "card stage column"},
            h.div({"class": "spread"},
                h.div({"class": "column"},
                    h.h2({"style": "--type: 2"}, name),
                    h.span({"style": "--fg: -0.5"}, course or "")),
                h.div({"class": "column", "style": "align-items: end"},
                    (h.span({"class": "tag suc"}, "in game") if is_in_game
                     else h.form({"method": "post",
                                  "action": f"/games/{game_id}/join",
                                  "style": "margin: 0"},
                            h.button({"class": "btn", "type": "submit"},
                                     "join game"))),
                    h.span({"style": "--type: -2; --fg: -0.5"},
                           f"{len(players)} / 4 players"))),
            h.table(
                Safe('<colgroup><col style="inline-size: 3.5em">' +
                     '<col>' * len(players) + '</colgroup>'),
                h.thead(h.tr(*header_cells)),
                h.tbody(*body_rows))),

        (h.div({"class": "card stage column"},
            h.h3({"style": "--type: 0"}, "add a guest"),
            h.p({"style": "--type: -1; --fg: -0.5"},
                f"{open_seats} seat{'s' if open_seats != 1 else ''} open. "
                "anyone in the game can edit guest scores."),
            h.form({"method": "post",
                    "action": f"/games/{game_id}/guests",
                    "class": "row"},
                h.input({"class": "input", "name": "name",
                         "placeholder": "guest name",
                         "required": True}),
                h.button({"class": "btn", "type": "submit"}, "add guest")))
         if can_add_guest else h.div()),

        # Popover: ignore-morph so SSE re-renders don't kill an open dialog.
        h.div({"data-ignore-morph": ""}, score_picker(game_id)))


def get_scorecard(req):
    sc = short_circuit(req)
    if sc: return sc
    game_id = int(req["params"]["id"])
    game = get_game(game_id)
    if not game:
        return error(404, "game not found")
    return html(h_render(full_page(
        f"scorecard — {game[1]}",
        header_bar(req, [("games", "/games"), (game[1], None)]),
        h.main({"class": "pg-main",
                "style": "max-inline-size: 56rem; margin: 1lh auto",
                "data-signals": '{"_pick":""}',
                "data-init": f"@get('/games/{game_id}/stream')"},
            render_scorecard(req, game_id)))))


def get_scorecard_stream(req):
    sc = short_circuit(req)
    if sc: return sc
    game_id = int(req["params"]["id"])
    resource = f"game-{game_id}"

    def render_frame(count, mode, interval_ms=None):
        main_attrs = {"class": "pg-main",
                      "style": "max-inline-size: 56rem; margin: 1lh auto",
                      "data-signals": '{"_pick":""}'}
        if mode == "poll":
            main_attrs[f"data-on-interval__duration.{interval_ms}ms"] = (
                f"@get('/games/{game_id}/stream')")
        game = get_game(game_id)
        title = game[1] if game else "scorecard"
        doc = full_page(
            f"scorecard — {title}",
            header_bar(req, [("games", "/games"), (title, None),
                             (f"{mode} · {count}", None)]),
            h.main(main_attrs, render_scorecard(req, game_id)))
        return f"event: datastar-patch-elements\ndata: elements {h_render(doc)}"

    if not live.should_be_live(resource):
        count = live.count(resource)
        interval = live.poll_interval_ms(resource)
        yield render_frame(count, "poll", interval)
        return

    with live.join(resource):
        yield render_frame(live.count(resource), "live")
        while True:
            db.changes.wait(timeout=15)
            try:
                yield render_frame(live.count(resource), "live")
            except (OSError, BrokenPipeError):
                return


def post_score(req):
    sc = short_circuit(req)
    if sc: return sc
    try:
        game_id   = int(req["params"]["id"])
        player_id = int(req["params"]["pid"])
        hole      = int(req["params"]["hole"])
        value     = int(req["params"]["v"])
    except (KeyError, ValueError):
        return error(400, "bad params")

    if not (1 <= hole <= NUM_HOLES):
        return error(400, "hole out of range")
    if not (0 <= value <= 9):
        return error(400, "strokes out of range (0-9; 0 clears)")

    owner = get_player_owner(player_id)
    if not owner:
        return error(404, "player not found")
    player_user_id, player_game_id = owner
    if player_game_id != game_id:
        return error(400, "player not in this game")

    uid = current_user_id(req)
    if player_user_id is not None and player_user_id != uid:
        return error(403, "not your row to edit")

    strokes = None if value == 0 else value
    set_score(game_id, player_id, hole, strokes)
    return (200, [("content-type", "text/plain")], b"")


# ─── routes ──────────────────────────────────────────────────────────

ROUTES = [
    ("GET",  "/healthz",
        lambda req: (200, [("content-type","text/plain")], b"ok")),
    ("GET",  "/",                                      get_root),
    ("POST", "/login",                                 post_login),
    ("GET",  "/me",                                    get_me),
    ("POST", "/me",                                    post_me),
    ("GET",  "/logout",                                get_logout),
    ("GET",  "/games",                                 get_games),
    ("GET",  "/games/stream",                          get_games_stream),
    ("POST", "/games",                                 post_games),
    ("POST", "/games/{id}/join",                       post_join),
    ("POST", "/games/{id}/guests",                     post_guest),
    ("GET",  "/games/{id}",                            get_scorecard),
    ("GET",  "/games/{id}/stream",                     get_scorecard_stream),
    ("POST", "/games/{id}/score/{pid}/{hole}/{v}",     post_score),
]


if __name__ == "__main__":
    serve(ROUTES,
          host=os.environ.get("HOST", "0.0.0.0"),
          port=int(os.environ.get("PORT", "8001")),
          before_hooks=[gate])
