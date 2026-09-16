import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import socket
import ipaddress
import time
from urllib.parse import urlencode, urlparse, urljoin
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from flask import (
    Flask, Response, jsonify, redirect, render_template, request,
    session, url_for
)

import requests
import qrcode


from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable
from reportlab.pdfgen import canvas as pdf_canvas

APP_TITLE = os.getenv("APP_TITLE", "Stiftungskalender")
APP_VERSION = "6"
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "stiftungskalender.sqlite"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = max(5, int(os.getenv("BACKUP_KEEP", "50")))
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
ICAL_TOKEN = os.getenv("ICAL_TOKEN", "")
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
ICAL_EXPORT_NAME = os.getenv("ICAL_EXPORT_NAME", "Stiftungskalender").strip() or "Stiftungskalender"

if AUTH_ENABLED and not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD muss gesetzt sein, wenn AUTH_ENABLED=true ist.")

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)



backup_lock = threading.Lock()


class YearOverviewCell(Flowable):
    """Compact annual-plan cell with optional caregiver fill and period rail."""

    def __init__(self, width, height, entry=None, periods=None, continuation=None):
        super().__init__()
        self.width = width
        self.height = height
        self.entry = entry
        self.periods = periods or []
        self.continuation = continuation

    def wrap(self, availWidth, availHeight):
        return self.width, self.height

    def draw(self):
        c = self.canv
        inset_x = 0.7
        inset_y = 0.7
        rail_w = self.width * 0.18 if self.periods else 0
        gap = 0.8 if self.periods else 0
        fill_w = self.width - (2 * inset_x) - rail_w - gap
        fill_h = self.height - (2 * inset_y)
        continuation_h = fill_h * 0.27 if self.continuation else 0
        continuation_gap = 0.55 if self.continuation and self.entry else 0
        entry_h = fill_h - continuation_h - continuation_gap if self.continuation else fill_h

        if self.entry:
            try:
                bg = colors.HexColor(self.entry.get("color") or "#ececec")
            except Exception:
                bg = colors.HexColor("#ececec")
            c.setFillColor(bg)
            c.roundRect(inset_x, inset_y, max(1, fill_w), max(1, entry_h), 2.0, stroke=0, fill=1)

            label = str(self.entry.get("person") or "")
            max_text_w = max(5, fill_w - 3)
            font_size = 4.8 if not self.continuation else 4.15
            min_font = 3.1 if self.continuation else 3.4
            while font_size > min_font and c.stringWidth(label, "Helvetica-Bold", font_size) > max_text_w:
                font_size -= 0.25
            if c.stringWidth(label, "Helvetica-Bold", font_size) > max_text_w:
                while len(label) > 2 and c.stringWidth(label + "…", "Helvetica-Bold", font_size) > max_text_w:
                    label = label[:-1]
                label += "…"
            c.setFont("Helvetica-Bold", font_size)
            c.setFillColor(colors.HexColor("#1e2524"))
            c.drawCentredString(inset_x + fill_w / 2, inset_y + (entry_h - font_size) / 2 + 0.7, label)

        if self.continuation:
            try:
                continuation_bg = colors.HexColor(self.continuation.get("color") or "#ececec")
            except Exception:
                continuation_bg = colors.HexColor("#ececec")
            continuation_y = self.height - inset_y - continuation_h
            c.setFillColor(continuation_bg)
            c.roundRect(inset_x, continuation_y, max(1, fill_w), max(1, continuation_h), 1.5, stroke=0, fill=1)
            continuation_text = str(self.continuation.get("continuation_text") or "").strip()
            continuation_label = str(self.continuation.get("person") or "")
            if continuation_text.startswith("bis "):
                continuation_label = f"{continuation_label} · {continuation_text}"
            max_text_w = max(5, fill_w - 2)
            continuation_font = 3.35
            while continuation_font > 2.45 and c.stringWidth(continuation_label, "Helvetica-Bold", continuation_font) > max_text_w:
                continuation_font -= 0.2
            if c.stringWidth(continuation_label, "Helvetica-Bold", continuation_font) > max_text_w:
                while len(continuation_label) > 3 and c.stringWidth(continuation_label + "…", "Helvetica-Bold", continuation_font) > max_text_w:
                    continuation_label = continuation_label[:-1]
                continuation_label += "…"
            c.setFont("Helvetica-Bold", continuation_font)
            c.setFillColor(colors.HexColor("#1e2524"))
            c.drawCentredString(inset_x + fill_w / 2, continuation_y + max(0.2, (continuation_h - continuation_font) / 2 + 0.25), continuation_label)

        if self.periods:
            x = self.width - inset_x - rail_w
            rail_h = fill_h / len(self.periods)
            for idx, period in enumerate(self.periods):
                try:
                    color = colors.HexColor(period.get("color") or "#f2a65a")
                except Exception:
                    color = colors.HexColor("#f2a65a")
                c.setFillColor(color)
                y = inset_y + idx * rail_h
                c.rect(x, y, rail_w, rail_h + 0.15, stroke=0, fill=1)


def year_export_filename(year):
    return f"jahresplan-{year}.pdf"


def _legend_item(label, color_value, width=42 * mm):
    try:
        swatch = colors.HexColor(color_value)
    except Exception:
        swatch = colors.HexColor("#ececec")
    label_style = ParagraphStyle(
        "LegendLabel",
        fontName="Helvetica",
        fontSize=5.5,
        leading=6.0,
        textColor=colors.HexColor("#1e2524"),
    )
    item = Table(
        [["", Paragraph(xml_escape(label), label_style)]],
        colWidths=[4 * mm, width - 4 * mm],
        rowHeights=[3.7 * mm],
    )
    item.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), swatch),
        ("BOX", (0, 0), (0, 0), 0.3, colors.HexColor("#cfd5d2")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return item

DEFAULT_PEOPLE = [
    ("Vreni", "#e9def7", 10),
    ("Kita", "#e2ecef", 20),
    ("Silvia", "#dff0df", 30),
    ("Annie/Sepp", "#f6e0db", 40),
    ("Janin/Hörbi", "#f4e7c9", 50),
    ("Silvia/Selli", "#d7efdf", 60),
    ("Andere", "#ececec", 999),
]

SYSTEM_PERSON_NAME = "__SYSTEM__"
GLOBAL_EVENT_COLOR = "#dcebe7"
COMPANY_SEED_COUNT = max(0, min(100, int(os.getenv("COMPANY_SEED_COUNT", "20"))))
COMPANY_PALETTE = [
    "#dbeafe", "#dcfce7", "#fef3c7", "#fce7f3", "#ede9fe",
    "#cffafe", "#fee2e2", "#e2e8f0", "#ecfccb", "#fae8ff",
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS people (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          color TEXT NOT NULL DEFAULT '#ececec',
          sort_order INTEGER NOT NULL DEFAULT 100,
          ical_title TEXT NOT NULL DEFAULT '',
          calendar_token TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS companies (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          color TEXT NOT NULL DEFAULT '#e2e8f0',
          sort_order INTEGER NOT NULL DEFAULT 100,
          calendar_token TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT NOT NULL,
          end_day TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          applies_to_all INTEGER NOT NULL DEFAULT 1,
          person_id INTEGER NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          all_day INTEGER NOT NULL DEFAULT 1,
          start_time TEXT NOT NULL DEFAULT '',
          end_time TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS periods (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          start_day TEXT NOT NULL,
          end_day TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'vacation',
          label TEXT NOT NULL DEFAULT '',
          color TEXT NOT NULL DEFAULT '#f2a65a',
          source TEXT NOT NULL DEFAULT 'manual',
          external_uid TEXT UNIQUE,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_periods_range ON periods(start_day, end_day);

        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS calendar_subscriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          url TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'holiday',
          color TEXT NOT NULL DEFAULT '#d65a6f',
          enabled INTEGER NOT NULL DEFAULT 1,
          last_sync_at TEXT NOT NULL DEFAULT '',
          last_status TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          action TEXT NOT NULL,
          entry_id INTEGER,
          snapshot TEXT NOT NULL DEFAULT '{}',
          before_snapshot TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);

        """)

        history_columns = {r["name"] for r in conn.execute("PRAGMA table_info(history)").fetchall()}
        if "before_snapshot" not in history_columns:
            conn.execute("ALTER TABLE history ADD COLUMN before_snapshot TEXT NOT NULL DEFAULT '{}'")

        people_columns = {r["name"] for r in conn.execute("PRAGMA table_info(people)").fetchall()}
        if "ical_title" not in people_columns:
            conn.execute("ALTER TABLE people ADD COLUMN ical_title TEXT NOT NULL DEFAULT ''")
        if "calendar_token" not in people_columns:
            conn.execute("ALTER TABLE people ADD COLUMN calendar_token TEXT NOT NULL DEFAULT ''")

        # Earlier schema revisions enforced one entry per day. This calendar needs
        # multiple independent appointments on the same date, so rebuild once.
        entry_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
        entry_sql = str(entry_sql_row["sql"] or "") if entry_sql_row else ""
        old_columns = {r["name"] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
        needs_rebuild = "UNIQUE" in entry_sql.upper()
        if needs_rebuild:
            conn.execute("ALTER TABLE entries RENAME TO entries_v64_legacy")
            conn.execute("""
                CREATE TABLE entries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  day TEXT NOT NULL,
                  end_day TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL DEFAULT '',
                  applies_to_all INTEGER NOT NULL DEFAULT 1,
                  person_id INTEGER NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  all_day INTEGER NOT NULL DEFAULT 1,
                  start_time TEXT NOT NULL DEFAULT '',
                  end_time TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE RESTRICT
                )
            """)
            title_expr = "title" if "title" in old_columns else "''"
            all_expr = "applies_to_all" if "applies_to_all" in old_columns else "1"
            end_day_expr = "end_day" if "end_day" in old_columns else "''"
            all_day_expr = "all_day" if "all_day" in old_columns else "1"
            start_expr = "start_time" if "start_time" in old_columns else "''"
            end_expr = "end_time" if "end_time" in old_columns else "''"
            conn.execute(f"""
                INSERT INTO entries(id,day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at)
                SELECT id,day,{end_day_expr},{title_expr},{all_expr},person_id,note,{all_day_expr},{start_expr},{end_expr},created_at,updated_at
                  FROM entries_v64_legacy
            """)
            conn.execute("DROP TABLE entries_v64_legacy")
        else:
            entry_columns = {r["name"] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
            if "all_day" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN all_day INTEGER NOT NULL DEFAULT 1")
            if "start_time" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN start_time TEXT NOT NULL DEFAULT ''")
            if "end_time" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN end_time TEXT NOT NULL DEFAULT ''")
            if "end_day" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN end_day TEXT NOT NULL DEFAULT ''")
            if "title" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if "applies_to_all" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN applies_to_all INTEGER NOT NULL DEFAULT 1")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_day ON entries(day)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_end_day ON entries(end_day)")

        # Normalize existing rows. Timed entries with an end time earlier
        # than the start time represented an overnight appointment ending next day.
        conn.execute("""
            UPDATE entries
               SET end_day = CASE
                   WHEN all_day=0 AND start_time<>'' AND end_time<>'' AND end_time < start_time
                     THEN date(day, '+1 day')
                   ELSE day
               END
             WHERE end_day='' OR end_day IS NULL
        """)

        conn.execute("UPDATE entries SET title='Termin' WHERE title='' OR title IS NULL")
        conn.execute("UPDATE entries SET applies_to_all=1 WHERE applies_to_all IS NULL")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS entry_companies (
          entry_id INTEGER NOT NULL,
          company_id INTEGER NOT NULL,
          PRIMARY KEY(entry_id, company_id),
          FOREIGN KEY(entry_id) REFERENCES entries(id) ON DELETE CASCADE,
          FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_entry_companies_company ON entry_companies(company_id, entry_id);
        """)

        # Internal system row required by the compact entries schema.
        row = conn.execute("SELECT id FROM people WHERE name=?", (SYSTEM_PERSON_NAME,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO people(name,color,sort_order,ical_title,calendar_token) VALUES(?,?,?,?,?)",
                (SYSTEM_PERSON_NAME, GLOBAL_EVENT_COLOR, 999999, "", secrets.token_urlsafe(32)),
            )


        # A fresh installation starts with 20 editable company placeholders.
        company_count = conn.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
        if company_count == 0 and COMPANY_SEED_COUNT:
            rows = []
            for idx in range(COMPANY_SEED_COUNT):
                rows.append((f"Unternehmen {idx+1:02d}", COMPANY_PALETTE[idx % len(COMPANY_PALETTE)], (idx+1)*10, secrets.token_urlsafe(32)))
            conn.executemany("INSERT INTO companies(name,color,sort_order,calendar_token) VALUES(?,?,?,?)", rows)

        for row in conn.execute("SELECT id,calendar_token FROM companies").fetchall():
            if not row["calendar_token"]:
                conn.execute("UPDATE companies SET calendar_token=? WHERE id=?", (secrets.token_urlsafe(32), row["id"]))

def backup_db(reason="change"):
    """Create a consistent standalone SQLite backup using SQLite's backup API."""
    with backup_lock:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dst = BACKUP_DIR / f"stiftungskalender-{stamp}-{reason}.sqlite"
        src = sqlite3.connect(DB_PATH, timeout=10)
        target = sqlite3.connect(dst)
        try:
            src.backup(target)
        finally:
            target.close()
            src.close()

        backups = sorted(BACKUP_DIR.glob("stiftungskalender-*.sqlite"), reverse=True)
        for old in backups[BACKUP_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not AUTH_ENABLED:
            return fn(*args, **kwargs)
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "not_authenticated"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


@app.get("/healthz")
def healthz():
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}, 200
    except Exception:
        return {"status": "error"}, 500


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = request.form.get("user", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(user, APP_USER) and hmac.compare_digest(password, APP_PASSWORD):
            session.clear()
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Benutzername oder Passwort ist falsch."
    return render_template("login.html", title=APP_TITLE, error=error)


@app.post("/logout")
def logout():
    session.clear()
    response = redirect(url_for("login") if AUTH_ENABLED else url_for("index"))
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


@app.get("/")
@login_required
def index():
    return render_template("index.html", title=APP_TITLE, auth_enabled=AUTH_ENABLED)



def valid_iso_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except Exception:
        return None


def valid_hhmm(value):
    value = str(value or "").strip()
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except Exception:
        return None


def normalize_entry_timing(payload, allow_equal=False):
    raw_all_day = payload.get("all_day", True)
    if isinstance(raw_all_day, str):
        all_day = raw_all_day.strip().lower() not in {"0", "false", "no", "off"}
    else:
        all_day = bool(raw_all_day)
    if all_day:
        return 1, "", "", None

    start_time = valid_hhmm(payload.get("start_time"))
    end_time = valid_hhmm(payload.get("end_time"))
    if not start_time or not end_time:
        return None, None, None, "Von und Bis sind bei einem Termin mit Uhrzeit erforderlich"
    if end_time == start_time and not allow_equal:
        return None, None, None, "Von und Bis dürfen nicht gleich sein"
    return 0, start_time, end_time, None


def resolve_entry_end_day(day, all_day, start_time, end_time, raw_end_day=None):
    """Resolve the inclusive calendar date on which a timed entry ends.

    Missing end_day keeps backwards compatibility: an end time earlier than the
    start time means the following day. New clients send end_day explicitly and
    can therefore create arbitrary multi-day intervals.
    """
    if all_day:
        return day, None

    raw = str(raw_end_day or "").strip()
    if raw:
        end_day = valid_iso_day(raw)
        if not end_day:
            return None, "Bis-Datum ist ungültig"
    else:
        end_obj = date.fromisoformat(day)
        if end_time < start_time:
            end_obj += timedelta(days=1)
        end_day = end_obj.isoformat()

    if end_day < day:
        return None, "Bis-Datum darf nicht vor dem Von-Datum liegen"
    if end_day == day and end_time <= start_time:
        return None, "Bis muss nach Von liegen"
    if (date.fromisoformat(end_day) - date.fromisoformat(day)).days > 3660:
        return None, "Ein Termin darf maximal zehn Jahre umfassen"
    return end_day, None


def entry_effective_end_day(row):
    day = str(row.get("day") or "")
    explicit = valid_iso_day(str(row.get("end_day") or ""))
    if explicit:
        return explicit
    if not row.get("all_day") and row.get("start_time") and row.get("end_time") and row["end_time"] < row["start_time"]:
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return day


def _entry_company_map_with_conn(conn, entry_ids):
    if not entry_ids:
        return {}
    placeholders = ",".join("?" for _ in entry_ids)
    rows = conn.execute(
        f"""SELECT ec.entry_id,c.id,c.name,c.color
              FROM entry_companies ec
              JOIN companies c ON c.id=ec.company_id
             WHERE ec.entry_id IN ({placeholders})
             ORDER BY c.sort_order,c.name""",
        tuple(entry_ids),
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row["entry_id"], []).append({"id": row["id"], "name": row["name"], "color": row["color"]})
    return result


def entry_rows(where="", params=()):
    query = """
      SELECT e.id, e.day, e.end_day, e.title, e.applies_to_all, e.person_id, e.note,
             e.all_day, e.start_time, e.end_time, e.created_at, e.updated_at
      FROM entries e
    """
    if where:
        query += " WHERE " + where
    query += " ORDER BY e.day ASC, e.start_time ASC, e.id ASC"
    with db() as conn:
        base = [dict(r) for r in conn.execute(query, params).fetchall()]
        company_map = _entry_company_map_with_conn(conn, [r["id"] for r in base])
    for row in base:
        companies = company_map.get(row["id"], [])
        title = str(row.get("title") or "Termin").strip() or "Termin"
        row["title"] = title
        row["person"] = title  # internal compatibility alias for shared UI helpers
        row["ical_title"] = ""
        row["companies"] = companies
        row["company_ids"] = [c["id"] for c in companies]
        row["company_names"] = [c["name"] for c in companies]
        row["scope_label"] = "Alle Unternehmen" if row.get("applies_to_all") else ", ".join(row["company_names"])
        if row.get("applies_to_all"):
            row["color"] = GLOBAL_EVENT_COLOR
        elif len(companies) == 1:
            row["color"] = companies[0]["color"]
        elif companies:
            row["color"] = companies[0]["color"]
        else:
            row["color"] = "#e2e8f0"
    return base


def public_app_url():
    """Public HTTPS base URL used for shareable calendar links."""
    if APP_URL:
        parsed = urlparse(APP_URL)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise RuntimeError("APP_URL muss eine vollständige HTTPS-URL sein, z. B. https://kalender.example.ch")
        return APP_URL
    return f"https://{request.host.split(':')[0]}"


def ical_feed_url(person_id=None, person_token=None, company_id=None, company_token=None):
    base = public_app_url() + "/calendar.ics"
    if not ICAL_TOKEN:
        return None
    if company_id is not None and company_token:
        params = {"company_id": int(company_id), "token": company_token}
    elif person_id is not None and person_token:  # internal compatibility
        params = {"person_id": int(person_id), "token": person_token}
    else:
        params = {"token": ICAL_TOKEN}
    return base + "?" + urlencode(params)

def qr_png_response(value):
    image = qrcode.make(value)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    response = Response(buf.getvalue(), mimetype="image/png")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@app.after_request
def harden_calendar_response(response):
    if request.path == "/calendar.ics":
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def get_setting(key, default=""):
    with db() as conn:
        row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))

def system_person_id_with_conn(conn):
    row = conn.execute("SELECT id FROM people WHERE name=?", (SYSTEM_PERSON_NAME,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO people(name,color,sort_order,ical_title,calendar_token) VALUES(?,?,?,?,?)",
        (SYSTEM_PERSON_NAME, GLOBAL_EVENT_COLOR, 999999, "", secrets.token_urlsafe(32)),
    )
    return int(cur.lastrowid)


def normalize_entry_scope_with_conn(conn, payload):
    title = str(payload.get("title") or "").strip()[:160]
    if not title:
        return None, None, None, "Termintitel fehlt"
    applies_to_all = 1 if payload.get("applies_to_all", True) else 0
    raw_ids = payload.get("company_ids") or []
    try:
        company_ids = sorted({int(x) for x in raw_ids})
    except (TypeError, ValueError):
        return None, None, None, "Ungültige Unternehmensauswahl"
    if applies_to_all:
        company_ids = []
    elif not company_ids:
        return None, None, None, "Mindestens ein Unternehmen auswählen oder 'Alle Unternehmen' verwenden"
    if company_ids:
        placeholders = ",".join("?" for _ in company_ids)
        found = {int(r["id"]) for r in conn.execute(f"SELECT id FROM companies WHERE id IN ({placeholders})", tuple(company_ids)).fetchall()}
        if found != set(company_ids):
            return None, None, None, "Mindestens ein ausgewähltes Unternehmen existiert nicht mehr"
    return title, applies_to_all, company_ids, None


def set_entry_companies_with_conn(conn, entry_id, company_ids):
    conn.execute("DELETE FROM entry_companies WHERE entry_id=?", (entry_id,))
    if company_ids:
        conn.executemany("INSERT INTO entry_companies(entry_id,company_id) VALUES(?,?)", [(entry_id, company_id) for company_id in company_ids])


HISTORY_ENTRY_FIELDS = ("day", "end_day", "title", "applies_to_all", "company_ids", "note", "all_day", "start_time", "end_time")

def history_add(action, snapshot, entry_id=None, before_snapshot=None):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO history(action,entry_id,snapshot,before_snapshot,created_at) VALUES(?,?,?,?,?)",
            (
                action,
                entry_id,
                json.dumps(snapshot or {}, ensure_ascii=False),
                json.dumps(before_snapshot or {}, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        history_id = cur.lastrowid
        conn.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 100)")
    return history_id

def entry_snapshot_with_conn(conn, entry_id):
    r=conn.execute(
        "SELECT e.id,e.day,e.end_day,e.title,e.applies_to_all,e.person_id,e.note,e.all_day,e.start_time,e.end_time "
        "FROM entries e WHERE e.id=?",
        (entry_id,),
    ).fetchone()
    if not r:
        return None
    snap = dict(r)
    companies = _entry_company_map_with_conn(conn, [entry_id]).get(entry_id, [])
    snap["company_ids"] = [c["id"] for c in companies]
    snap["company_names"] = [c["name"] for c in companies]
    snap["scope_label"] = "Alle Unternehmen" if snap.get("applies_to_all") else ", ".join(snap["company_names"])
    snap["person"] = snap.get("title") or "Termin"
    return snap

def entry_snapshot(entry_id):
    with db() as conn:
        return entry_snapshot_with_conn(conn, entry_id)

def entry_snapshots_differ(before, after):
    before = before or {}
    after = after or {}
    return any(before.get(key) != after.get(key) for key in HISTORY_ENTRY_FIELDS)


@app.get("/api/history")
@login_required
def api_history():
    with db() as conn:
        rows = conn.execute("SELECT id,action,entry_id,snapshot,before_snapshot,created_at FROM history ORDER BY id DESC LIMIT 20").fetchall()
    result=[]
    for row in rows:
        try:
            snapshot=json.loads(row["snapshot"] or "{}")
            if not isinstance(snapshot, dict): snapshot={}
        except Exception:
            snapshot={}
        try:
            before_snapshot=json.loads(row["before_snapshot"] or "{}")
            if not isinstance(before_snapshot, dict): before_snapshot={}
        except Exception:
            before_snapshot={}
        result.append({
            "id":row["id"],
            "action":row["action"],
            "entry_id":row["entry_id"],
            "snapshot":snapshot,
            "after":snapshot,
            "before":before_snapshot,
            "created_at":row["created_at"],
        })
    return jsonify(result)


@app.post("/api/history/<int:history_id>/restore")
@login_required
def api_history_restore(history_id):
    with db() as conn:
        row=conn.execute("SELECT * FROM history WHERE id=?",(history_id,)).fetchone()
        if not row: return jsonify({"error":"Änderung nicht gefunden"}),404
        try: snap=json.loads(row["snapshot"] or "{}")
        except Exception: return jsonify({"error":"Gespeicherter Termin ist ungültig"}),400
        day=valid_iso_day(str(snap.get("day","")))
        if not day: return jsonify({"error":"Gespeichertes Datum ist ungültig"}),400
        all_day=1 if snap.get("all_day",1) else 0; start_time=str(snap.get("start_time","") or ""); end_time=str(snap.get("end_time","") or "")
        restored_end,end_error=resolve_entry_end_day(day,all_day,start_time,end_time,snap.get("end_day"))
        if end_error: return jsonify({"error":f"Gespeicherter Termin ist ungültig: {end_error}"}),400
        title=str(snap.get("title") or snap.get("person") or "Termin").strip() or "Termin"
        applies=1 if snap.get("applies_to_all",True) else 0
        company_ids=[] if applies else [int(x) for x in (snap.get("company_ids") or [])]
        if not applies and not company_ids: return jsonify({"error":"Die frühere Unternehmenszuordnung ist nicht mehr verfügbar"}),409
        person_id=system_person_id_with_conn(conn); now=datetime.now().isoformat(timespec="seconds")
        cur=conn.execute("""INSERT INTO entries(day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (day,restored_end,title,applies,person_id,str(snap.get("note", "")),all_day,start_time,end_time,now,now))
        restored_id=cur.lastrowid; set_entry_companies_with_conn(conn,restored_id,company_ids)
    history_add("restored",entry_snapshot(restored_id),restored_id); backup_db("history-restore")
    return jsonify({"ok":True,"id":restored_id})


@app.get("/api/config")
@login_required
def api_config():
    company_urls=[]
    if ICAL_TOKEN:
        with db() as conn:
            companies=conn.execute("SELECT id,name,color,calendar_token FROM companies ORDER BY sort_order,name").fetchall()
        company_urls=[
            {"id":r["id"],"name":r["name"],"color":r["color"],
             "url":ical_feed_url(company_id=r["id"],company_token=r["calendar_token"]),
             "qr_url":f"/api/companies/{r['id']}/calendar-qr.png"}
            for r in companies
        ]
    return jsonify({
        "title":APP_TITLE,
        "version":APP_VERSION,
        "mode":"desktop",
        "ical_enabled":bool(ICAL_TOKEN),
        "ical_url":ical_feed_url() if ICAL_TOKEN else None,
        "ical_qr_url":"/api/calendar-qr.png" if ICAL_TOKEN else None,
        "ical_company_urls":company_urls,
        "data_file":str(DB_PATH),"backup_dir":str(BACKUP_DIR),"auth_enabled":AUTH_ENABLED,
    })


@app.get("/api/companies")
@login_required
def api_companies():
    with db() as conn:
        rows = conn.execute("SELECT id,name,color,sort_order FROM companies ORDER BY sort_order,name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/companies")
@login_required
def api_companies_add():
    payload = request.get_json(force=True)
    name = str(payload.get("name", "")).strip()[:120]
    color = valid_color(payload.get("color"), "#e2e8f0")
    if not name:
        return jsonify({"error": "Unternehmensname fehlt"}), 400
    try:
        with db() as conn:
            max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM companies").fetchone()["m"]
            cur = conn.execute(
                "INSERT INTO companies(name,color,sort_order,calendar_token) VALUES(?,?,?,?)",
                (name, color, int(max_order or 0) + 10, secrets.token_urlsafe(32)),
            )
        backup_db("company-add")
        return jsonify({"id": cur.lastrowid, "name": name, "color": color}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Dieses Unternehmen existiert bereits"}), 409


@app.post("/api/companies/bulk")
@login_required
def api_companies_bulk():
    payload = request.get_json(force=True)
    names = payload.get("names") or []
    if not isinstance(names, list):
        return jsonify({"error": "Ungültige Unternehmensliste"}), 400
    clean=[]; seen=set()
    for value in names[:100]:
        name=str(value or "").strip()[:120]
        if name and name.lower() not in seen:
            seen.add(name.lower()); clean.append(name)
    if not clean:
        return jsonify({"error": "Keine Unternehmensnamen angegeben"}),400
    created=0
    with db() as conn:
        max_order=int(conn.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM companies").fetchone()["m"] or 0)
        existing={str(r["name"]).lower() for r in conn.execute("SELECT name FROM companies").fetchall()}
        for idx,name in enumerate(clean):
            if name.lower() in existing: continue
            conn.execute("INSERT INTO companies(name,color,sort_order,calendar_token) VALUES(?,?,?,?)",
                         (name, COMPANY_PALETTE[(max_order//10+idx)%len(COMPANY_PALETTE)], max_order+(idx+1)*10, secrets.token_urlsafe(32)))
            created+=1
    if created: backup_db("company-bulk")
    return jsonify({"ok":True,"created":created})


@app.put("/api/companies/<int:company_id>")
@login_required
def api_companies_update(company_id):
    payload=request.get_json(force=True)
    name=str(payload.get("name","")).strip()[:120]
    color=valid_color(payload.get("color"),"#e2e8f0")
    if not name: return jsonify({"error":"Unternehmensname fehlt"}),400
    try:
        with db() as conn:
            if not conn.execute("SELECT 1 FROM companies WHERE id=?",(company_id,)).fetchone():
                return jsonify({"error":"Unternehmen nicht gefunden"}),404
            conn.execute("UPDATE companies SET name=?,color=? WHERE id=?",(name,color,company_id))
        backup_db("company-update")
        return jsonify({"ok":True})
    except sqlite3.IntegrityError:
        return jsonify({"error":"Dieses Unternehmen existiert bereits"}),409


@app.delete("/api/companies/<int:company_id>")
@login_required
def api_companies_delete(company_id):
    with db() as conn:
        used=conn.execute("SELECT COUNT(*) AS c FROM entry_companies WHERE company_id=?",(company_id,)).fetchone()["c"]
        if used:
            return jsonify({"error":f"Unternehmen ist noch {used} Termin(en) zugeordnet"}),409
        conn.execute("DELETE FROM companies WHERE id=?",(company_id,))
    backup_db("company-delete")
    return jsonify({"ok":True})


@app.post("/api/companies/<int:company_id>/calendar-token/reset")
@login_required
def api_company_calendar_token_reset(company_id):
    token=secrets.token_urlsafe(32)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM companies WHERE id=?",(company_id,)).fetchone():
            return jsonify({"error":"Unternehmen nicht gefunden"}),404
        conn.execute("UPDATE companies SET calendar_token=? WHERE id=?",(token,company_id))
    backup_db("company-calendar-token-reset")
    return jsonify({"ok":True})


@app.get("/api/companies/<int:company_id>/calendar-qr.png")
@login_required
def api_company_calendar_qr(company_id):
    with db() as conn:
        row=conn.execute("SELECT calendar_token FROM companies WHERE id=?",(company_id,)).fetchone()
    if not row or not ICAL_TOKEN: return Response("Not found",status=404)
    return qr_png_response(ical_feed_url(company_id=company_id,company_token=row["calendar_token"]))


@app.get("/api/entries")
@login_required
def api_entries_list():
    return jsonify(entry_rows())


def _entry_payload_with_conn(conn, payload):
    day = valid_iso_day(str(payload.get("day", "")))
    if not day:
        return None, "Datum ist ungültig"
    title, applies_to_all, company_ids, scope_error = normalize_entry_scope_with_conn(conn, payload)
    if scope_error:
        return None, scope_error
    all_day, start_time, end_time, timing_error = normalize_entry_timing(payload)
    if timing_error:
        return None, timing_error
    end_day, end_error = resolve_entry_end_day(day, all_day, start_time, end_time, payload.get("end_day"))
    if end_error:
        return None, end_error
    return {
        "day": day,
        "end_day": end_day,
        "title": title,
        "applies_to_all": applies_to_all,
        "company_ids": company_ids,
        "note": str(payload.get("note", "") or "").strip()[:4000],
        "all_day": all_day,
        "start_time": start_time,
        "end_time": end_time,
    }, None


@app.post("/api/entries")
@login_required
def api_entries_create():
    payload = request.get_json(force=True)
    with db() as conn:
        values, error = _entry_payload_with_conn(conn, payload)
        if error:
            return jsonify({"error": error}), 400
        now = datetime.now().isoformat(timespec="seconds")
        person_id = system_person_id_with_conn(conn)
        cur = conn.execute(
            """INSERT INTO entries(day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (values["day"], values["end_day"], values["title"], values["applies_to_all"], person_id,
             values["note"], values["all_day"], values["start_time"], values["end_time"], now, now),
        )
        entry_id = int(cur.lastrowid)
        set_entry_companies_with_conn(conn, entry_id, values["company_ids"])
    history_add("created", entry_snapshot(entry_id), entry_id)
    backup_db("entry-create")
    return jsonify({"ok": True, "id": entry_id}), 201


@app.put("/api/entries/<int:entry_id>")
@login_required
def api_entries_update(entry_id):
    payload = request.get_json(force=True)
    with db() as conn:
        before = entry_snapshot_with_conn(conn, entry_id)
        if not before:
            return jsonify({"error": "Termin nicht gefunden"}), 404
        values, error = _entry_payload_with_conn(conn, payload)
        if error:
            return jsonify({"error": error}), 400
        conn.execute(
            """UPDATE entries SET day=?,end_day=?,title=?,applies_to_all=?,note=?,all_day=?,start_time=?,end_time=?,updated_at=? WHERE id=?""",
            (values["day"], values["end_day"], values["title"], values["applies_to_all"], values["note"],
             values["all_day"], values["start_time"], values["end_time"], datetime.now().isoformat(timespec="seconds"), entry_id),
        )
        set_entry_companies_with_conn(conn, entry_id, values["company_ids"])
    after = entry_snapshot(entry_id)
    if entry_snapshots_differ(before, after):
        history_add("updated", after, entry_id, before_snapshot=before)
        backup_db("entry-update")
    return jsonify({"ok": True, "id": entry_id})


@app.delete("/api/entries/<int:entry_id>")
@login_required
def api_entries_delete(entry_id):
    with db() as conn:
        snapshot = entry_snapshot_with_conn(conn, entry_id)
        if not snapshot:
            return jsonify({"error": "Termin nicht gefunden"}), 404
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    history_add("deleted", snapshot, entry_id)
    backup_db("entry-delete")
    return jsonify({"ok": True})


def _batch_plan_with_conn(conn, payload):
    title, applies_to_all, company_ids, scope_error = normalize_entry_scope_with_conn(conn, payload)
    if scope_error:
        return None, scope_error
    start_day = valid_iso_day(str(payload.get("start_day", "")))
    end_day = valid_iso_day(str(payload.get("end_day", "")))
    if not start_day or not end_day:
        return None, "Von und Bis sind erforderlich"
    if end_day < start_day:
        return None, "Bis darf nicht vor Von liegen"
    start_obj = date.fromisoformat(start_day)
    end_obj = date.fromisoformat(end_day)
    if (end_obj - start_obj).days > 3660:
        return None, "Zeitraum ist zu groß"
    try:
        weekday = int(payload.get("weekday"))
    except (TypeError, ValueError):
        weekday = -1
    if weekday < 0 or weekday > 6:
        return None, "Wochentag ist ungültig"
    all_day, start_time, end_time, timing_error = normalize_entry_timing(payload)
    if timing_error:
        return None, timing_error

    matches=[]
    d=start_obj
    while d<=end_obj:
        if d.weekday()==weekday:
            matches.append(d.isoformat())
        d += timedelta(days=1)

    period_rows_all = conn.execute("SELECT start_day,end_day,kind FROM periods WHERE end_day>=? AND start_day<=?", (start_day,end_day)).fetchall()
    skip_vac=bool(payload.get("skip_vacations"))
    skip_hol=bool(payload.get("skip_holidays"))
    create_days=[]; vacation_count=0; holiday_count=0
    for day in matches:
        kinds={str(r["kind"] or "") for r in period_rows_all if r["start_day"]<=day<=r["end_day"]}
        skip=False
        if "vacation" in kinds and skip_vac:
            vacation_count+=1; skip=True
        if "holiday" in kinds and skip_hol:
            holiday_count+=1; skip=True
        if not skip: create_days.append(day)

    return {
        "title":title,"applies_to_all":applies_to_all,"company_ids":company_ids,
        "note":str(payload.get("note", "") or "").strip()[:4000],
        "all_day":all_day,"start_time":start_time,"end_time":end_time,
        "matched_days":matches,"create_days":create_days,
        "vacation_count":vacation_count,"holiday_count":holiday_count,
    }, None


@app.post("/api/entries/batch/preview")
@login_required
def api_entries_batch_preview():
    payload=request.get_json(force=True)
    with db() as conn:
        plan,error=_batch_plan_with_conn(conn,payload)
    if error: return jsonify({"error":error}),400
    return jsonify({
        "matched_count":len(plan["matched_days"]),
        "create_count":len(plan["create_days"]),
        "vacation_count":plan["vacation_count"],
        "holiday_count":plan["holiday_count"],
    })


@app.post("/api/entries/batch")
@login_required
def api_entries_batch_create():
    payload=request.get_json(force=True)
    with db() as conn:
        plan,error=_batch_plan_with_conn(conn,payload)
        if error: return jsonify({"error":error}),400
        person_id=system_person_id_with_conn(conn)
        now=datetime.now().isoformat(timespec="seconds")
        created_ids=[]
        for day in plan["create_days"]:
            end_day,_=resolve_entry_end_day(day,plan["all_day"],plan["start_time"],plan["end_time"],None)
            cur=conn.execute(
                """INSERT INTO entries(day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (day,end_day,plan["title"],plan["applies_to_all"],person_id,plan["note"],plan["all_day"],plan["start_time"],plan["end_time"],now,now),
            )
            eid=int(cur.lastrowid); set_entry_companies_with_conn(conn,eid,plan["company_ids"]); created_ids.append(eid)
    for eid in created_ids:
        history_add("created",entry_snapshot(eid),eid)
    if created_ids: backup_db("entry-batch")
    return jsonify({"ok":True,"created_count":len(created_ids),"skipped_count":len(plan["matched_days"])-len(created_ids)})


def period_rows(year=""):
    """Return stored periods, optionally limited to periods overlapping a year."""
    year = str(year or "").strip()
    query = "SELECT id,start_day,end_day,kind,label,color,source,external_uid,created_at,updated_at FROM periods"
    params = ()
    if re.fullmatch(r"\d{4}", year):
        query += " WHERE start_day <= ? AND end_day >= ?"
        params = (f"{year}-12-31", f"{year}-01-01")
    query += " ORDER BY start_day ASC,end_day ASC,id ASC"
    with db() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


@app.get("/api/periods")
@login_required
def api_periods():
    year = request.args.get("year", "").strip()
    return jsonify(period_rows(year))


@app.post("/api/periods")
@login_required
def api_periods_add():
    payload = request.get_json(force=True)
    start_day = valid_iso_day(str(payload.get("start_day", "")))
    end_day = valid_iso_day(str(payload.get("end_day", "")))
    kind = str(payload.get("kind", "vacation")).strip() or "vacation"
    label = str(payload.get("label", "")).strip()
    default_color = "#f2a65a" if kind == "vacation" else "#d65a6f" if kind == "holiday" else "#80a4c2"
    color = valid_color(payload.get("color"), default_color)
    if not start_day or not end_day:
        return jsonify({"error": "Start- und Enddatum sind erforderlich"}), 400
    if end_day < start_day:
        return jsonify({"error": "Enddatum muss nach dem Startdatum liegen"}), 400
    if not label:
        label = "Ferien" if kind == "vacation" else "Feiertag" if kind == "holiday" else "Zeitraum"
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO periods(start_day,end_day,kind,label,color,source,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual',?,?)""",
            (start_day, end_day, kind, label, color, now, now),
        )
        period_id = cur.lastrowid
    backup_db("period-add")
    return jsonify({"id": period_id}), 201


@app.put("/api/periods/<int:period_id>")
@login_required
def api_periods_update(period_id):
    payload = request.get_json(force=True)
    start_day = valid_iso_day(str(payload.get("start_day", "")))
    end_day = valid_iso_day(str(payload.get("end_day", "")))
    kind = str(payload.get("kind", "vacation")).strip() or "vacation"
    label = str(payload.get("label", "")).strip()
    default_color = "#f2a65a" if kind == "vacation" else "#d65a6f" if kind == "holiday" else "#80a4c2"
    color = valid_color(payload.get("color"), default_color)
    if not start_day or not end_day or end_day < start_day:
        return jsonify({"error": "Ungültiger Zeitraum"}), 400
    if not label:
        label = "Ferien" if kind == "vacation" else "Feiertag" if kind == "holiday" else "Zeitraum"
    with db() as conn:
        conn.execute(
            """UPDATE periods SET start_day=?,end_day=?,kind=?,label=?,color=?,updated_at=?
               WHERE id=?""",
            (start_day, end_day, kind, label, color, datetime.now().isoformat(timespec="seconds"), period_id),
        )
    backup_db("period-update")
    return jsonify({"ok": True})


@app.delete("/api/periods/<int:period_id>")
@login_required
def api_periods_delete(period_id):
    with db() as conn:
        conn.execute("DELETE FROM periods WHERE id=?", (period_id,))
    backup_db("period-delete")
    return jsonify({"ok": True})


def unfold_ics_lines(raw):
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def ics_unescape(value):
    return (str(value or "")
            .replace("\\n", "\n")
            .replace("\\N", "\n")
            .replace("\\,", ",")
            .replace("\\;", ";")
            .replace("\\\\", "\\"))


def parse_ics_date(value):
    value = str(value or "").strip()
    match = re.search(r"(\d{8})", value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def parse_ics_events(raw):
    events = []
    current = None
    for line in unfold_ics_lines(raw):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        name = left.split(";", 1)[0].upper()
        current[name] = (left, value)
    return events


def expand_ics_dates(event):
    dtstart_prop = event.get("DTSTART")
    if not dtstart_prop:
        return []
    start_obj = parse_ics_date(dtstart_prop[1])
    if not start_obj:
        return []

    dtend_prop = event.get("DTEND")
    end_obj = parse_ics_date(dtend_prop[1]) if dtend_prop else start_obj
    if not end_obj:
        end_obj = start_obj
    if dtend_prop and "VALUE=DATE" in dtend_prop[0].upper() and end_obj > start_obj:
        end_obj -= timedelta(days=1)
    if end_obj < start_obj:
        end_obj = start_obj
    duration = end_obj - start_obj

    rrule_prop = event.get("RRULE")
    if not rrule_prop or "FREQ=YEARLY" not in rrule_prop[1].upper():
        return [(start_obj, end_obj, None)]

    rule = {}
    for part in rrule_prop[1].split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            rule[key.upper()] = value

    month = int(rule.get("BYMONTH", start_obj.month))
    monthday = int(rule.get("BYMONTHDAY", start_obj.day))
    until_obj = parse_ics_date(rule.get("UNTIL", "")) if rule.get("UNTIL") else None
    current_year = date.today().year
    first_year = max(start_obj.year, current_year - 1)
    last_year = current_year + 5
    if until_obj:
        last_year = min(last_year, until_obj.year)
    dates = []
    for year in range(first_year, last_year + 1):
        try:
            occurrence_start = date(year, month, monthday)
        except ValueError:
            continue
        if occurrence_start < start_obj:
            continue
        if until_obj and occurrence_start > until_obj:
            continue
        dates.append((occurrence_start, occurrence_start + duration, year))
    return dates


def _subscription_url_is_safe(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False, "Ungültige URL"
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return False, "Nur HTTP/HTTPS-URLs sind erlaubt"
    if parsed.username or parsed.password:
        return False, "Benutzername/Passwort in der URL werden nicht unterstützt"
    if parsed.scheme == "http" and os.getenv("ALLOW_HTTP_CALENDAR_SUBSCRIPTIONS", "false").lower() not in {"1","true","yes","on"}:
        return False, "Kalender-Abos müssen HTTPS verwenden"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False, "Interne/private Zieladressen sind nicht erlaubt"
    except Exception:
        return False, "Kalender-Host konnte nicht aufgelöst werden"
    return True, ""


def fetch_subscription_ics(url):
    current = url
    for _ in range(4):
        safe, error = _subscription_url_is_safe(current)
        if not safe:
            raise ValueError(error)
        response = requests.get(current, timeout=(5, 20), allow_redirects=False, headers={"User-Agent": "Stiftungskalender/1", "Accept": "text/calendar,text/plain;q=0.9,*/*;q=0.2"})
        if response.status_code in {301,302,303,307,308}:
            location = response.headers.get("Location")
            if not location:
                raise ValueError("Kalender-Weiterleitung ohne Ziel")
            current = urljoin(current, location)
            continue
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError(f"Kalender-Server antwortet mit HTTP {response.status_code}")
        if len(response.content) > 5 * 1024 * 1024:
            raise ValueError("Kalender ist größer als 5 MB")
        try:
            return response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return response.content.decode("latin-1")
    raise ValueError("Zu viele Weiterleitungen")


def sync_calendar_subscription(subscription_id):
    with db() as conn:
        sub = conn.execute("SELECT * FROM calendar_subscriptions WHERE id=?", (subscription_id,)).fetchone()
    if not sub:
        raise ValueError("Kalender-Abo nicht gefunden")
    if not sub["enabled"]:
        return {"imported": 0, "skipped": 0, "disabled": True}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        raw = fetch_subscription_ics(sub["url"])
        imported = 0
        skipped = 0
        keep_uids = []
        source = f"subscription:{subscription_id}"
        with db() as conn:
            for event in parse_ics_events(raw):
                occurrences = expand_ics_dates(event)
                if not occurrences:
                    skipped += 1
                    continue
                summary = ics_unescape(event.get("SUMMARY", ("SUMMARY", "Kalendertermin"))[1]).strip() or "Kalendertermin"
                uid = ics_unescape(event.get("UID", ("UID", ""))[1]).strip()
                for start_obj, end_obj, recurrence_year in occurrences:
                    base_uid = uid if uid else f"{start_obj.isoformat()}:{end_obj.isoformat()}:{summary}"
                    external_uid = f"sub:{subscription_id}:" + base_uid + (f":{recurrence_year}" if recurrence_year else "")
                    keep_uids.append(external_uid)
                    conn.execute(
                        """INSERT INTO periods(start_day,end_day,kind,label,color,source,external_uid,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(external_uid) DO UPDATE SET
                             start_day=excluded.start_day,end_day=excluded.end_day,kind=excluded.kind,
                             label=excluded.label,color=excluded.color,source=excluded.source,updated_at=excluded.updated_at""",
                        (start_obj.isoformat(), end_obj.isoformat(), sub["kind"], summary, sub["color"], source, external_uid, now, now),
                    )
                    imported += 1
            if keep_uids:
                placeholders = ",".join("?" for _ in keep_uids)
                conn.execute(f"DELETE FROM periods WHERE source=? AND external_uid NOT IN ({placeholders})", (source, *keep_uids))
            else:
                conn.execute("DELETE FROM periods WHERE source=?", (source,))
            conn.execute("UPDATE calendar_subscriptions SET last_sync_at=?,last_status=?,updated_at=? WHERE id=?", (now, f"OK · {imported} Termine", now, subscription_id))
        return {"imported": imported, "skipped": skipped}
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE calendar_subscriptions SET last_sync_at=?,last_status=?,updated_at=? WHERE id=?", (now, f"Fehler · {str(exc)[:180]}", now, subscription_id))
        raise


def subscription_rows():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM calendar_subscriptions ORDER BY name,id").fetchall()]


@app.get("/api/calendar-subscriptions")
@login_required
def api_calendar_subscriptions():
    return jsonify(subscription_rows())


@app.post("/api/calendar-subscriptions")
@login_required
def api_calendar_subscriptions_add():
    payload = request.get_json(force=True)
    name = str(payload.get("name", "")).strip()
    url = str(payload.get("url", "")).strip()
    kind = str(payload.get("kind", "holiday")).strip() or "holiday"
    if kind not in {"holiday","vacation","other"}:
        kind = "other"
    default_color = "#d65a6f" if kind == "holiday" else "#f2a65a" if kind == "vacation" else "#80a4c2"
    color = valid_color(payload.get("color"), default_color)
    if not name or not url:
        return jsonify({"error": "Name und Kalender-URL sind erforderlich"}), 400
    safe, error = _subscription_url_is_safe(url)
    if not safe:
        return jsonify({"error": error}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cur = conn.execute("INSERT INTO calendar_subscriptions(name,url,kind,color,enabled,last_sync_at,last_status,created_at,updated_at) VALUES(?,?,?,?,1,'','Noch nicht synchronisiert',?,?)", (name,url,kind,color,now,now))
        sub_id = cur.lastrowid
    try:
        result = sync_calendar_subscription(sub_id)
    except Exception as exc:
        result = {"warning": str(exc)}
    backup_db("calendar-subscription-add")
    return jsonify({"id": sub_id, **result}), 201


@app.put("/api/calendar-subscriptions/<int:subscription_id>")
@login_required
def api_calendar_subscriptions_update(subscription_id):
    payload = request.get_json(force=True)
    with db() as conn:
        current = conn.execute("SELECT * FROM calendar_subscriptions WHERE id=?", (subscription_id,)).fetchone()
    if not current:
        return jsonify({"error": "Kalender-Abo nicht gefunden"}), 404
    name = str(payload.get("name", current["name"])).strip()
    url = str(payload.get("url", current["url"])).strip()
    kind = str(payload.get("kind", current["kind"])).strip()
    color = valid_color(payload.get("color", current["color"]), current["color"])
    enabled = 1 if bool(payload.get("enabled", current["enabled"])) else 0
    safe, error = _subscription_url_is_safe(url)
    if not safe:
        return jsonify({"error": error}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("UPDATE calendar_subscriptions SET name=?,url=?,kind=?,color=?,enabled=?,updated_at=? WHERE id=?", (name,url,kind,color,enabled,now,subscription_id))
    if enabled:
        try: sync_calendar_subscription(subscription_id)
        except Exception: pass
    else:
        with db() as conn: conn.execute("DELETE FROM periods WHERE source=?", (f"subscription:{subscription_id}",))
    backup_db("calendar-subscription-update")
    return jsonify({"ok": True})


@app.post("/api/calendar-subscriptions/<int:subscription_id>/sync")
@login_required
def api_calendar_subscriptions_sync(subscription_id):
    try:
        result = sync_calendar_subscription(subscription_id)
        backup_db("calendar-subscription-sync")
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/calendar-subscriptions/<int:subscription_id>")
@login_required
def api_calendar_subscriptions_delete(subscription_id):
    with db() as conn:
        conn.execute("DELETE FROM periods WHERE source=?", (f"subscription:{subscription_id}",))
        conn.execute("DELETE FROM calendar_subscriptions WHERE id=?", (subscription_id,))
    backup_db("calendar-subscription-delete")
    return jsonify({"ok": True})


def sync_all_calendar_subscriptions():
    for sub in subscription_rows():
        if sub["enabled"]:
            try: sync_calendar_subscription(sub["id"])
            except Exception: pass


def calendar_subscription_worker():
    # Delay startup so the web process becomes responsive immediately.
    time.sleep(15)
    interval = max(1, int(os.getenv("SUBSCRIPTION_SYNC_HOURS", "12"))) * 3600
    while True:
        sync_all_calendar_subscriptions()
        time.sleep(interval)


@app.post("/import.ics")
@login_required
def import_ics():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Keine ICS-Datei ausgewählt"}), 400
    raw_bytes = file.read()
    try:
        raw = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw = raw_bytes.decode("latin-1")

    kind = request.form.get("kind", "holiday").strip() or "holiday"
    default_color = "#d65a6f" if kind == "holiday" else "#f2a65a"
    color = valid_color(request.form.get("color"), default_color)
    imported = 0
    skipped = 0
    now = datetime.now().isoformat(timespec="seconds")

    with db() as conn:
        for event in parse_ics_events(raw):
            occurrences = expand_ics_dates(event)
            if not occurrences:
                skipped += 1
                continue

            summary = ics_unescape(event.get("SUMMARY", ("SUMMARY", "Feiertag"))[1]).strip() or "Feiertag"
            uid = ics_unescape(event.get("UID", ("UID", ""))[1]).strip()

            for start_obj, end_obj, recurrence_year in occurrences:
                base_uid = uid if uid else f"{start_obj.isoformat()}:{end_obj.isoformat()}:{summary}"
                external_uid = "ics:" + base_uid + (f":{recurrence_year}" if recurrence_year else "")

                conn.execute(
                    """INSERT INTO periods(start_day,end_day,kind,label,color,source,external_uid,created_at,updated_at)
                       VALUES(?,?,?,?,?,'ics',?,?,?)
                       ON CONFLICT(external_uid) DO UPDATE SET
                         start_day=excluded.start_day,
                         end_day=excluded.end_day,
                         kind=excluded.kind,
                         label=excluded.label,
                         color=excluded.color,
                         source='ics',
                         updated_at=excluded.updated_at""",
                    (start_obj.isoformat(), end_obj.isoformat(), kind, summary, color, external_uid, now, now),
                )
                imported += 1

    if imported:
        backup_db("ics-import")
    return jsonify({"ok": True, "imported": imported, "skipped": skipped})

def filtered_entry_rows(year="", companies=None, search=""):
    companies=[c for c in (companies or []) if c]
    clauses=[]; params=[]
    if year:
        clauses.append("e.day LIKE ?"); params.append(f"{year}-%")
    if companies:
        placeholders=",".join("?" for _ in companies)
        clauses.append(f"(e.applies_to_all=1 OR EXISTS (SELECT 1 FROM entry_companies ec JOIN companies c ON c.id=ec.company_id WHERE ec.entry_id=e.id AND c.name IN ({placeholders})))")
        params.extend(companies)
    if search:
        needle=f"%{search.lower()}%"
        clauses.append("(LOWER(e.title) LIKE ? OR LOWER(e.note) LIKE ? OR EXISTS (SELECT 1 FROM entry_companies ec JOIN companies c ON c.id=ec.company_id WHERE ec.entry_id=e.id AND LOWER(c.name) LIKE ?))")
        params.extend([needle,needle,needle])
    return entry_rows(" AND ".join(clauses),tuple(params)) if clauses else entry_rows()


def export_filename(extension, year="", companies=None):
    companies=[c for c in (companies or []) if c]; parts=["stiftungskalender"]
    if len(companies)==1:
        safe="".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in companies[0]).strip("-")
        if safe: parts.append(safe)
    elif len(companies)>1: parts.append(f"auswahl-{len(companies)}")
    if year: parts.append(year)
    if len(parts)==1: parts.append("alle")
    return "-".join(parts)+extension

@app.get("/export.csv")
@login_required
def export_csv():
    year=request.args.get("year","").strip(); companies=[x.strip() for x in request.args.getlist("company") if x.strip()]; search=request.args.get("q","").strip()
    raw_months=request.args.getlist("month"); months=sorted({int(m) for m in raw_months if m.isdigit() and 1<=int(m)<=12})
    rows=filtered_entry_rows(year,companies,search)
    if months and year:
        prefixes=tuple(f"{year}-{m:02d}-" for m in months); rows=[r for r in rows if r["day"].startswith(prefixes)]
    buf=io.StringIO(); w=csv.writer(buf,delimiter=";")
    w.writerow(["Datum","Bis-Datum","Termin","Unternehmen","Ganztägig","Von","Bis","Beschreibung"])
    for r in rows:
        w.writerow([r["day"],entry_effective_end_day(r),r["title"],r["scope_label"],"Ja" if r["all_day"] else "Nein",r["start_time"],r["end_time"],r["note"]])
    body="\ufeff"+buf.getvalue()
    return Response(body,mimetype="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{export_filename(".csv",year,companies)}"'})

@app.get("/export.pdf")
@login_required
def export_pdf():
    year = request.args.get("year", "").strip()
    companies = [p.strip() for p in request.args.getlist("company") if p.strip()]
    search = request.args.get("q", "").strip()
    rows = filtered_entry_rows(year, companies, search)

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Stiftungskalender {', '.join(companies) if companies else 'Alle Unternehmen'} {year}".strip(),
        author=APP_TITLE,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ListTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=19,
        textColor=colors.HexColor("#1e2524"),
        spaceAfter=4 * mm,
    )
    meta_style = ParagraphStyle(
        "ListMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#68716f"),
        spaceAfter=5 * mm,
    )
    cell_style = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=10.5,
        textColor=colors.HexColor("#1e2524"),
    )
    header_style = ParagraphStyle(
        "Header",
        parent=cell_style,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    if not companies:
        label = "Alle Unternehmen"
    elif len(companies) <= 3:
        label = ", ".join(companies)
    else:
        label = f"{len(companies)} ausgewählte Unternehmen"
    if year:
        label += f" - {year}"
    if search:
        label += f" - Filter: {search}"

    story = [
        Paragraph("Stiftungskalender", title_style),
        Paragraph(f"{xml_escape(label)}<br/>{len(rows)} Termine", meta_style),
    ]

    data = [[
        Paragraph("Datum", header_style),
        Paragraph("Tag", header_style),
        Paragraph("Termin", header_style),
        Paragraph("Unternehmen", header_style),
        Paragraph("Zeit", header_style),
        Paragraph("Bemerkung", header_style),
    ]]
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    row_colors = []
    for idx, r in enumerate(rows, start=1):
        day_obj = date.fromisoformat(r["day"])
        data.append([
            Paragraph(day_obj.strftime("%d.%m.%Y"), cell_style),
            Paragraph(weekdays[day_obj.weekday()], cell_style),
            Paragraph(xml_escape(r["title"]), cell_style),
            Paragraph(xml_escape(r["scope_label"]), cell_style),
            Paragraph(
                "Ganztägig" if r["all_day"] else
                (f"{r['start_time']}–{r['end_time']}" if entry_effective_end_day(r) == r["day"]
                 else f"{r['start_time']} → {date.fromisoformat(entry_effective_end_day(r)).strftime('%d.%m.%Y')} {r['end_time']}"),
                cell_style
            ),
            Paragraph(xml_escape(r["note"] or ""), cell_style),
        ])
        try:
            row_colors.append((idx, colors.HexColor(r["color"])))
        except Exception:
            row_colors.append((idx, colors.HexColor("#ececec")))

    table = Table(
        data,
        colWidths=[23 * mm, 12 * mm, 40 * mm, 40 * mm, 28 * mm, 45 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305f57")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dfe4e1")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index, bg in row_colors:
        table_style.append(("BACKGROUND", (2, row_index), (2, row_index), bg))
    table.setStyle(TableStyle(table_style))
    story.append(table)

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#68716f"))
        canvas.drawString(12 * mm, 7 * mm, APP_TITLE)
        canvas.drawRightString(A4[0] - 12 * mm, 7 * mm, f"Seite {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    pdf_bytes = output.getvalue()
    output.close()
    filename = export_filename(".pdf", year, companies)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store",
        },
    )


@app.get("/export-year.pdf")
@login_required
def export_year_pdf():
    raw_year = request.args.get("year", "").strip()
    try:
        year = int(raw_year)
    except ValueError:
        year = date.today().year
    if year < 1900 or year > 2200:
        year = date.today().year

    raw_months = request.args.getlist("month")
    selected_months = sorted({int(m) for m in raw_months if m.isdigit() and 1 <= int(m) <= 12})

    month_names = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                   "Juli", "August", "September", "Oktober", "November", "Dezember"]
    month_indices = selected_months if selected_months else list(range(1, 13))
    single_month = len(month_indices) == 1

    range_start = date(year, 1, 1).isoformat()
    range_end = date(year, 12, 31).isoformat()
    company_filter = request.args.get("company", "").strip()
    source_entries = entry_rows("e.day <= ? AND COALESCE(NULLIF(e.end_day,''), e.day) >= ?", (range_end, range_start))
    if company_filter:
        source_entries = [
            row for row in source_entries
            if int(row.get("applies_to_all") or 0) == 1 or company_filter in (row.get("company_names") or [])
        ]
    year_entries = [row for row in source_entries if row["day"].startswith(f"{year}-")]
    if selected_months:
        prefixes = tuple(f"{year}-{m:02d}-" for m in month_indices)
        display_entries = [row for row in year_entries if row["day"].startswith(prefixes)]
    else:
        display_entries = year_entries

    def aggregate_year_cell(rows, continuation=False):
        if not rows:
            return None
        first = dict(rows[0])
        titles = [str(r.get("title") or r.get("person") or "Termin") for r in rows]
        label = " / ".join(titles[:2]) + (f" +{len(titles)-2}" if len(titles) > 2 else "")
        first["person"] = label
        first["title"] = label
        if len(rows) > 1:
            first["color"] = GLOBAL_EVENT_COLOR
        if continuation:
            final_rows = [r for r in rows if r.get("_continuation_final")]
            if len(rows) == 1 and final_rows and not int(rows[0].get("all_day") or 0):
                first["continuation_text"] = f"bis {rows[0].get('end_time') or ''}".strip()
            else:
                first["continuation_text"] = ""
        return first

    grouped_by_day = {}
    for row in display_entries:
        grouped_by_day.setdefault(row["day"], []).append(row)
    by_day = {day: aggregate_year_cell(rows) for day, rows in grouped_by_day.items()}

    continuation_groups = {}
    selected_month_set = set(month_indices)
    for row in source_entries:
        final_day = date.fromisoformat(entry_effective_end_day(row))
        current = date.fromisoformat(row["day"]) + timedelta(days=1)
        while current <= final_day:
            if current.year == year and current.month in selected_month_set:
                continuation = dict(row)
                continuation["_continuation_final"] = current == final_day
                continuation_groups.setdefault(current.isoformat(), []).append(continuation)
            current += timedelta(days=1)
    continuation_by_day = {day: aggregate_year_cell(rows, continuation=True) for day, rows in continuation_groups.items()}

    year_periods = period_rows(str(year))

    display_periods = []
    periods_by_day = {}
    for period in year_periods:
        period_start = date.fromisoformat(period["start_day"])
        period_end = date.fromisoformat(period["end_day"])
        if period_end < date(year, 1, 1) or period_start > date(year, 12, 31):
            continue
        period_used = False
        start_obj = max(period_start, date(year, 1, 1))
        end_obj = min(period_end, date(year, 12, 31))
        current = start_obj
        while current <= end_obj:
            if current.month in selected_month_set:
                periods_by_day.setdefault(current.isoformat(), []).append(period)
                period_used = True
            current += timedelta(days=1)
        if period_used:
            display_periods.append(period)

    # Direct canvas rendering is intentionally used here instead of a large
    # Platypus table.  The annual grid has a fixed geometry and direct drawing
    # avoids LayoutError exceptions when legends or row heights change.
    output = io.BytesIO()
    page_size = A4 if single_month else landscape(A3)
    page_w, page_h = page_size
    margin_x = 10 * mm if single_month else 8 * mm
    plan_title = (
        f"Monatsplan {month_names[month_indices[0] - 1]} {year}"
        if single_month
        else (f"Jahresplan {year}" if len(month_indices) == 12 else f"Monatsauswahl {year} · {len(month_indices)} Monate")
    )
    if company_filter:
        plan_title += f" · {company_filter}"

    legend_items = []
    if company_filter:
        with db() as conn:
            selected_company = conn.execute("SELECT name,color FROM companies WHERE name=?", (company_filter,)).fetchone()
        if selected_company:
            legend_items.append((f"{selected_company['name']} + globale Termine", selected_company["color"]))
        else:
            legend_items.append(("Globale Termine", GLOBAL_EVENT_COLOR))
    else:
        legend_items.append(("Alle Unternehmen", GLOBAL_EVENT_COLOR))
        legend_items.append(("Unternehmensbezogene Termine", "#e2e8f0"))
    legend_items.append(("Wochenende", "#fff2b9"))
    if continuation_by_day:
        legend_items.append(("Fortsetzung mehrtägiger Termin", "#e4efeb"))

    kind_names = {"vacation": "Ferien", "holiday": "Feiertage", "other": "Markierung"}
    seen_period_legend = set()
    for period in display_periods:
        key = (kind_names.get(period.get("kind"), period.get("label") or "Markierung"), period.get("color") or "#80a4c2")
        if key not in seen_period_legend:
            seen_period_legend.add(key)
            legend_items.append(key)

    per_row = 3 if single_month else 7
    legend_rows = max(1, (len(legend_items) + per_row - 1) // per_row)
    legend_line_h = 4.2 * mm
    legend_total_h = legend_rows * legend_line_h

    pdf = pdf_canvas.Canvas(output, pagesize=page_size)
    pdf.setTitle(plan_title)
    pdf.setAuthor(APP_TITLE)

    def safe_color(value, fallback="#ececec"):
        try:
            return colors.HexColor(value or fallback)
        except Exception:
            return colors.HexColor(fallback)

    def fitted_text(text, max_width, font_name="Helvetica-Bold", start_size=5.0, min_size=2.8):
        label = str(text or "")
        size = start_size
        while size > min_size and pdf.stringWidth(label, font_name, size) > max_width:
            size -= 0.2
        if pdf.stringWidth(label, font_name, size) > max_width:
            trimmed = label
            while len(trimmed) > 2 and pdf.stringWidth(trimmed + "…", font_name, size) > max_width:
                trimmed = trimmed[:-1]
            label = trimmed + "…"
        return label, size

    # Title
    pdf.setFillColor(colors.HexColor("#1e2524"))
    pdf.setFont("Helvetica-Bold", 11 if single_month else 13)
    pdf.drawString(margin_x, page_h - 9 * mm, plan_title)

    grid_left = margin_x
    grid_right = page_w - margin_x
    grid_top = page_h - 14 * mm
    grid_bottom = 8 * mm + legend_total_h + (1.5 * mm if legend_items else 0)
    grid_w = grid_right - grid_left
    grid_h = grid_top - grid_bottom
    header_h = 6 * mm if single_month else 6.0 * mm
    day_h = max(3.5 * mm, (grid_h - header_h) / 31.0)
    day_col_w = 10 * mm if single_month else 7.5 * mm
    month_w = (grid_w - 2 * day_col_w) / len(month_indices)

    # Header background + labels.
    pdf.setFillColor(colors.HexColor("#f5f7f6"))
    pdf.rect(grid_left, grid_top - header_h, grid_w, header_h, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor("#1e2524"))
    pdf.setFont("Helvetica-Bold", 6.2 if single_month else 6.2)
    pdf.drawCentredString(grid_left + day_col_w / 2, grid_top - header_h + 1.6 * mm, "Tag")
    for idx, month_idx in enumerate(month_indices):
        x = grid_left + day_col_w + idx * month_w
        label, fsize = fitted_text(month_names[month_idx - 1], month_w - 2, start_size=(6.2 if single_month else 6.2), min_size=4.0)
        pdf.setFont("Helvetica-Bold", fsize)
        pdf.drawCentredString(x + month_w / 2, grid_top - header_h + 1.6 * mm, label)
    pdf.setFont("Helvetica-Bold", 6.2 if single_month else 6.2)
    pdf.drawCentredString(grid_right - day_col_w / 2, grid_top - header_h + 1.6 * mm, "Tag")

    # Day rows and calendar cells.
    for day_num in range(1, 32):
        y_top = grid_top - header_h - (day_num - 1) * day_h
        y = y_top - day_h
        pdf.setFillColor(colors.HexColor("#fafbfa"))
        pdf.rect(grid_left, y, day_col_w, day_h, stroke=0, fill=1)
        pdf.rect(grid_right - day_col_w, y, day_col_w, day_h, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#1e2524"))
        pdf.setFont("Helvetica-Bold", 5.7 if single_month else 5.6)
        baseline = y + max(1.0, (day_h - (5.7 if single_month else 5.6)) / 2)
        pdf.drawCentredString(grid_left + day_col_w / 2, baseline, str(day_num))
        pdf.drawCentredString(grid_right - day_col_w / 2, baseline, str(day_num))

        for col_idx, month_idx in enumerate(month_indices):
            x = grid_left + day_col_w + col_idx * month_w
            try:
                d = date(year, month_idx, day_num)
            except ValueError:
                pdf.setFillColor(colors.HexColor("#f1f2f1"))
                pdf.rect(x, y, month_w, day_h, stroke=0, fill=1)
                continue

            iso = d.isoformat()
            if d.weekday() >= 5:
                pdf.setFillColor(colors.HexColor("#fff2b9"))
                pdf.rect(x, y, month_w, day_h, stroke=0, fill=1)

            direct_rows = grouped_by_day.get(iso, [])
            continuation_rows = continuation_groups.get(iso, [])
            periods = periods_by_day.get(iso, [])
            inset = 0.6
            rail_w = month_w * 0.14 if periods else 0
            gap = 0.6 if periods else 0
            content_w = max(1, month_w - 2 * inset - rail_w - gap)
            content_h = max(1, day_h - 2 * inset)

            cards = []
            for row in direct_rows:
                cards.append({
                    "row": row,
                    "label": str(row.get("title") or row.get("person") or "Termin"),
                    "continuation": False,
                })
            for row in continuation_rows:
                label = str(row.get("title") or row.get("person") or "Termin")
                if row.get("_continuation_final") and not int(row.get("all_day") or 0) and row.get("end_time"):
                    label += f" · bis {row.get('end_time')}"
                cards.append({"row": row, "label": label, "continuation": True})

            max_cards = 5 if single_month else 3
            if len(cards) > max_cards:
                visible_cards = cards[: max_cards - 1]
                hidden_count = len(cards) - len(visible_cards)
                display_cards = visible_cards + [{"more": hidden_count}]
            else:
                display_cards = cards

            if display_cards:
                card_gap = 0.45 if single_month else 0.35
                card_h = max(1.0, (content_h - card_gap * (len(display_cards) - 1)) / len(display_cards))
                for card_idx, card in enumerate(display_cards):
                    card_y = y + inset + (len(display_cards) - 1 - card_idx) * (card_h + card_gap)
                    if "more" in card:
                        pdf.setFillColor(colors.HexColor("#f5f7f6"))
                        pdf.setStrokeColor(colors.HexColor("#cfd8d4"))
                        pdf.setLineWidth(0.25)
                        pdf.roundRect(x + inset, card_y, content_w, card_h, 1.0, stroke=1, fill=1)
                        more_label = f"+{card['more']} weitere"
                        more_label, fsize = fitted_text(
                            more_label,
                            content_w - 2,
                            font_name="Helvetica-Bold",
                            start_size=(5.0 if single_month else 4.4),
                            min_size=3.0,
                        )
                        pdf.setFillColor(colors.HexColor("#596360"))
                        pdf.setFont("Helvetica-Bold", fsize)
                        pdf.drawCentredString(
                            x + inset + content_w / 2,
                            card_y + max(0.35, (card_h - fsize) / 2 + 0.35),
                            more_label,
                        )
                        continue

                    row = card["row"]
                    continuation = bool(card.get("continuation"))
                    pdf.setFillColor(safe_color(row.get("color"), "#e4efeb" if continuation else "#ececec"))
                    pdf.setStrokeColor(colors.HexColor("#8ba59d") if continuation else safe_color(row.get("color")))
                    pdf.setLineWidth(0.35 if continuation else 0.15)
                    pdf.roundRect(x + inset, card_y, content_w, card_h, 1.0, stroke=1 if continuation else 0, fill=1)
                    label, fsize = fitted_text(
                        card.get("label") or "Termin",
                        content_w - 2,
                        start_size=(5.2 if single_month else 4.6),
                        min_size=(3.2 if single_month else 3.0),
                    )
                    pdf.setFillColor(colors.HexColor("#1e2524"))
                    pdf.setFont("Helvetica-Bold", fsize)
                    pdf.drawCentredString(
                        x + inset + content_w / 2,
                        card_y + max(0.35, (card_h - fsize) / 2 + 0.35),
                        label,
                    )

            if periods:
                rail_x = x + month_w - inset - rail_w
                segment_h = content_h / len(periods)
                for p_idx, period in enumerate(periods):
                    pdf.setFillColor(safe_color(period.get("color"), "#80a4c2"))
                    pdf.rect(rail_x, y + inset + p_idx * segment_h, rail_w, segment_h + 0.15, stroke=0, fill=1)

    # Grid lines are drawn last so they remain crisp above fills.
    pdf.setStrokeColor(colors.HexColor("#dfe4e1"))
    pdf.setLineWidth(0.25)
    x_positions = [grid_left, grid_left + day_col_w]
    x_positions.extend(grid_left + day_col_w + i * month_w for i in range(1, len(month_indices) + 1))
    x_positions.append(grid_right)
    for x in x_positions:
        pdf.line(x, grid_bottom, x, grid_top)
    pdf.line(grid_left, grid_top, grid_right, grid_top)
    pdf.line(grid_left, grid_top - header_h, grid_right, grid_top - header_h)
    for i in range(1, 32):
        yy = grid_top - header_h - i * day_h
        pdf.line(grid_left, yy, grid_right, yy)

    # Compact legend below the grid.
    item_w = grid_w / per_row
    legend_top = grid_bottom - 1.2 * mm
    for idx, (label, color_value) in enumerate(legend_items):
        row_idx = idx // per_row
        col_idx = idx % per_row
        x = grid_left + col_idx * item_w
        y = legend_top - (row_idx + 1) * legend_line_h + 1.0 * mm
        pdf.setFillColor(safe_color(color_value))
        pdf.rect(x, y, 3.2 * mm, 2.6 * mm, stroke=0, fill=1)
        label_text, fsize = fitted_text(label, item_w - 4.3 * mm, font_name="Helvetica", start_size=5.4, min_size=3.8)
        pdf.setFillColor(colors.HexColor("#1e2524"))
        pdf.setFont("Helvetica", fsize)
        pdf.drawString(x + 4.0 * mm, y + 0.55 * mm, label_text)

    pdf.setFillColor(colors.HexColor("#68716f"))
    pdf.setFont("Helvetica", 5.5)
    pdf.drawString(margin_x, 3.2 * mm, APP_TITLE)
    pdf.drawRightString(page_w - margin_x, 3.2 * mm, plan_title)
    pdf.showPage()
    pdf.save()

    pdf_bytes = output.getvalue()
    output.close()
    filename = (
        f"monatsplan-{year}-{month_indices[0]:02d}.pdf"
        if single_month
        else (year_export_filename(year) if len(month_indices) == 12 else f"jahresplan-{year}-{len(month_indices)}-monate.pdf")
    )
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store",
        },
    )

@app.post("/import.csv")
@login_required
def import_csv():
    file=request.files.get("file")
    if not file: return jsonify({"error":"Keine Datei"}),400
    raw=file.read().decode("utf-8-sig"); sample=raw[:2048]
    try: dialect=csv.Sniffer().sniff(sample,delimiters=";,")
    except Exception:
        dialect=csv.excel; dialect.delimiter=";"
    reader=csv.DictReader(io.StringIO(raw),dialect=dialect); imported=0; now=datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        person_id=system_person_id_with_conn(conn)
        company_lookup={str(r["name"]).lower():int(r["id"]) for r in conn.execute("SELECT id,name FROM companies").fetchall()}
        for row in reader:
            day=valid_iso_day((row.get("Datum") or row.get("date") or row.get("day") or "").strip())
            title=(row.get("Termin") or row.get("Titel") or "").strip()
            note=(row.get("Beschreibung") or row.get("Bemerkung") or row.get("note") or "").strip()
            raw_all=(row.get("Ganztägig") or row.get("all_day") or "").strip().lower(); start_time=valid_hhmm(row.get("Von") or row.get("start_time")) or ""; end_time=valid_hhmm(row.get("Bis") or row.get("end_time")) or ""
            all_day=0 if (raw_all in {"nein","no","0","false","off"} or (not raw_all and start_time and end_time)) else 1
            if all_day: start_time=end_time=""
            if not day or not title or (not all_day and (not start_time or not end_time)): continue
            end_day,end_error=resolve_entry_end_day(day,all_day,start_time,end_time,(row.get("Bis-Datum") or row.get("end_day") or "").strip())
            if end_error: continue
            scope=(row.get("Unternehmen") or "").strip(); company_ids=[]; applies=1
            if scope and scope.lower() not in {"alle","alle unternehmen","global"}:
                names=[x.strip() for x in re.split(r"[,|]",scope) if x.strip()]
                company_ids=[company_lookup[n.lower()] for n in names if n.lower() in company_lookup]
                if company_ids: applies=0
            cur=conn.execute("""INSERT INTO entries(day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                             (day,end_day,title,applies,person_id,note,all_day,start_time,end_time,now,now))
            set_entry_companies_with_conn(conn,cur.lastrowid,company_ids); imported+=1
    if imported: backup_db("import")
    return jsonify({"ok":True,"imported":imported})

def ics_escape(value):
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


@app.get("/export-data.json")
@login_required
def export_data_json():
    with db() as conn:
        companies=[dict(r) for r in conn.execute("SELECT name,color,sort_order,calendar_token FROM companies ORDER BY sort_order,name").fetchall()]
        entries=entry_rows()
        portable_entries=[{k:r.get(k) for k in ("day","end_day","title","applies_to_all","company_names","note","all_day","start_time","end_time","created_at","updated_at")} for r in entries]
        periods=[dict(r) for r in conn.execute("SELECT start_day,end_day,kind,label,color,source,external_uid,created_at,updated_at FROM periods WHERE source NOT LIKE 'subscription:%' ORDER BY start_day,end_day,label").fetchall()]
        subscriptions=[dict(r) for r in conn.execute("SELECT name,url,kind,color,enabled,last_sync_at,last_status,created_at,updated_at FROM calendar_subscriptions ORDER BY name,id").fetchall()]
    payload={"format":"stiftungskalender-backup","version":1,"exported_at":datetime.now().isoformat(timespec="seconds"),"app_title":APP_TITLE,
             "companies":companies,"entries":portable_entries,"periods":periods,"calendar_subscriptions":subscriptions}
    raw=json.dumps(payload,ensure_ascii=False,indent=2).encode("utf-8")
    return Response(raw,mimetype="application/json; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="stiftungskalender-backup-{date.today().isoformat()}.json"'})


@app.post("/import-data.json")
@login_required
def import_data_json():
    file=request.files.get("file")
    if not file: return jsonify({"error":"Keine Backup-Datei ausgewählt"}),400
    raw=file.read(10*1024*1024+1)
    if len(raw)>10*1024*1024: return jsonify({"error":"Backup-Datei ist größer als 10 MB"}),400
    try: payload=json.loads(raw.decode("utf-8-sig"))
    except Exception: return jsonify({"error":"Ungültige JSON-Backup-Datei"}),400
    fmt=payload.get("format"); version=payload.get("version")
    if fmt!="stiftungskalender-backup": return jsonify({"error":"Unbekanntes Backup-Format"}),400
    if version!=1: return jsonify({"error":f"Nicht unterstützte Backup-Version: {version}"}),400

    companies=payload.get("companies") or []; entries=payload.get("entries") or []; periods=payload.get("periods") or []; subscriptions=payload.get("calendar_subscriptions") or []
    if not isinstance(companies,list) or not isinstance(entries,list): return jsonify({"error":"Backup ist unvollständig"}),400
    backup_db("before-full-import")
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE"); conn.execute("DELETE FROM entry_companies"); conn.execute("DELETE FROM entries"); conn.execute("DELETE FROM companies"); conn.execute("DELETE FROM periods"); conn.execute("DELETE FROM calendar_subscriptions")
            for idx,c in enumerate(companies):
                name=str(c.get("name","")).strip();
                if not name: continue
                conn.execute("INSERT INTO companies(name,color,sort_order,calendar_token) VALUES(?,?,?,?)",(name,valid_color(c.get("color"),"#e2e8f0"),int(c.get("sort_order",(idx+1)*10)),str(c.get("calendar_token") or secrets.token_urlsafe(32))))
            company_ids={str(r["name"]):int(r["id"]) for r in conn.execute("SELECT id,name FROM companies").fetchall()}; person_id=system_person_id_with_conn(conn)
            for item in entries:
                day=valid_iso_day(str(item.get("day", ""))); title=str(item.get("title") or "Termin").strip();
                if not day or not title: continue
                all_day,start_time,end_time,timing_error=normalize_entry_timing(item,allow_equal=True)
                if timing_error: continue
                end_day,_=resolve_entry_end_day(day,all_day,start_time,end_time,item.get("end_day")); applies=1 if item.get("applies_to_all",True) else 0
                names=[str(x) for x in (item.get("company_names") or []) if str(x) in company_ids]; applies=1 if (applies or not names) else 0
                now=datetime.now().isoformat(timespec="seconds"); cur=conn.execute("""INSERT INTO entries(day,end_day,title,applies_to_all,person_id,note,all_day,start_time,end_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (day,end_day,title,applies,person_id,str(item.get("note", "")),all_day,start_time,end_time,str(item.get("created_at") or now),str(item.get("updated_at") or now)))
                if not applies: set_entry_companies_with_conn(conn,cur.lastrowid,[company_ids[n] for n in names])
            for item in periods:
                sd=valid_iso_day(str(item.get("start_day", ""))); ed=valid_iso_day(str(item.get("end_day", "")))
                if not sd or not ed or ed<sd: continue
                now=datetime.now().isoformat(timespec="seconds"); conn.execute("INSERT INTO periods(start_day,end_day,kind,label,color,source,external_uid,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sd,ed,str(item.get("kind") or "other"),str(item.get("label") or "Zeitraum"),valid_color(item.get("color"),"#80a4c2"),str(item.get("source") or "manual"),item.get("external_uid"),str(item.get("created_at") or now),str(item.get("updated_at") or now)))
            for item in subscriptions:
                name=str(item.get("name", "")).strip(); url=str(item.get("url", "")).strip()
                if not name or not url: continue
                now=datetime.now().isoformat(timespec="seconds"); conn.execute("INSERT INTO calendar_subscriptions(name,url,kind,color,enabled,last_sync_at,last_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (name,url,str(item.get("kind") or "other"),valid_color(item.get("color"),"#80a4c2"),1 if item.get("enabled",True) else 0,str(item.get("last_sync_at", "")),str(item.get("last_status", "")),str(item.get("created_at") or now),str(item.get("updated_at") or now)))
    except sqlite3.Error as exc: return jsonify({"error":f"Import fehlgeschlagen: {exc}"}),400
    backup_db("after-full-import"); return jsonify({"ok":True,"companies":len(companies),"entries":len(entries),"periods":len(periods),"calendar_subscriptions":len(subscriptions)})

def ical_event_title(r):
    return str(r.get("title") or r.get("person") or "Termin")


def entry_ics_event_lines(r, host, uid_mode="feed"):
    """Build one VEVENT.

    Feed/subscription events use the stable UID ``stiftungskalender-<id>``.
    One-time/manual imports use the separate stable UID
    ``stiftungskalender-manual-<id>``. This prevents Apple Calendar from treating a
    manual import as the already-present read-only subscription event, while
    repeated manual imports of the same entry still identify the same event.
    """
    day_compact = r["day"].replace("-", "")
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uid_prefix = "stiftungskalender-manual" if uid_mode == "manual" else "stiftungskalender"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid_prefix}-{r['id']}@{host}",
        f"DTSTAMP:{dtstamp}",
    ]
    if r.get("all_day", 1):
        next_day = (date.fromisoformat(r["day"]) + timedelta(days=1)).strftime("%Y%m%d")
        lines.extend([
            f"DTSTART;VALUE=DATE:{day_compact}",
            f"DTEND;VALUE=DATE:{next_day}",
        ])
    else:
        start_compact = str(r.get("start_time") or "").replace(":", "")
        end_compact = str(r.get("end_time") or "").replace(":", "")
        end_day = date.fromisoformat(entry_effective_end_day(r))
        end_day_compact = end_day.strftime("%Y%m%d")
        lines.extend([
            f"DTSTART:{day_compact}T{start_compact}00",
            f"DTEND:{end_day_compact}T{end_compact}00",
        ])
    lines.extend([
        f"SUMMARY:{ics_escape(ical_event_title(r))}",
        f"DESCRIPTION:{ics_escape((r.get('scope_label') or "Alle Unternehmen") + ("\n" + r['note'] if r.get('note') else ""))}",
        "CATEGORIES:Stiftungskalender",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ])
    return lines


def build_ics_calendar(rows, host, calendar_name, uid_mode="feed"):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Stiftungskalender//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(calendar_name)}",
    ]
    for row in rows:
        lines.extend(entry_ics_event_lines(row, host, uid_mode=uid_mode))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def rows_for_calendar_range(start_day,end_day,companies=None,search=""):
    companies=[x for x in (companies or []) if x]; clauses=["e.day <= ?","COALESCE(NULLIF(e.end_day,''),e.day) >= ?"]; params=[end_day,start_day]
    if companies:
        placeholders=",".join("?" for _ in companies); clauses.append(f"(e.applies_to_all=1 OR EXISTS (SELECT 1 FROM entry_companies ec JOIN companies c ON c.id=ec.company_id WHERE ec.entry_id=e.id AND c.name IN ({placeholders})))"); params.extend(companies)
    if search:
        needle=f"%{search.lower()}%"; clauses.append("(LOWER(e.title) LIKE ? OR LOWER(e.note) LIKE ?)"); params.extend([needle,needle])
    rows=entry_rows(" AND ".join(clauses),tuple(params)); wanted_start=date.fromisoformat(start_day); wanted_end=date.fromisoformat(end_day)
    return [row for row in rows if date.fromisoformat(entry_effective_end_day(row))>=wanted_start and date.fromisoformat(row["day"])<=wanted_end]

@app.get("/export.ics")
@login_required
def export_ics_range():
    start_day = valid_iso_day(request.args.get("from", ""))
    end_day = valid_iso_day(request.args.get("to", ""))
    if not start_day or not end_day:
        return Response("Ungültiger Zeitraum", status=400)
    if start_day > end_day:
        return Response("Von-Datum liegt nach Bis-Datum", status=400)
    if (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days > 3660:
        return Response("Zeitraum ist zu groß", status=400)

    companies = [p.strip() for p in request.args.getlist("company") if p.strip()]
    search = request.args.get("q", "").strip()
    rows = rows_for_calendar_range(start_day, end_day, companies, search)
    host = request.host.split(":")[0]
    # The one-time ICS export uses a dedicated configurable calendar name.
    # This is independent from the individual event titles.
    calendar_name = ICAL_EXPORT_NAME
    body = build_ics_calendar(rows, host, calendar_name, uid_mode="manual")
    filename = f"stiftungskalender-{start_day}-bis-{end_day}.ics"
    disposition = "inline" if request.args.get("open") == "1" else "attachment"
    return Response(
        body,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/entries/<int:entry_id>/ics")
@login_required
def entry_ics_export(entry_id):
    rows = entry_rows("e.id = ?", (entry_id,))
    if not rows:
        return Response("Not found", status=404)
    row = rows[0]
    host = request.host.split(":")[0]
    body = build_ics_calendar([row], host, f"{APP_TITLE} – {row['title']}", uid_mode="manual")
    filename = f"stiftungskalender-{row['day']}-{entry_id}.ics"
    return Response(
        body,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


@app.get("/calendar.ics")
def calendar_ics():
    token=request.args.get("token",""); company_id_raw=request.args.get("company_id","").strip(); entry_id_raw=request.args.get("entry_id","").strip(); start_raw=request.args.get("from","").strip(); end_raw=request.args.get("to","").strip()
    company_row=None; company_id=None
    if company_id_raw:
        try: company_id=int(company_id_raw)
        except ValueError: return Response("Not found",status=404)
        with db() as conn: company_row=conn.execute("SELECT id,name,calendar_token FROM companies WHERE id=?",(company_id,)).fetchone()
        if not company_row or not hmac.compare_digest(token,company_row["calendar_token"]): return Response("Not found",status=404)
    elif not ICAL_TOKEN or not hmac.compare_digest(token,ICAL_TOKEN):
        return Response("Not found",status=404)

    filename="stiftungskalender.ics"; cal_name=f"{APP_TITLE} – {company_row['name']}" if company_row else APP_TITLE
    company_clause="(e.applies_to_all=1 OR EXISTS (SELECT 1 FROM entry_companies ec WHERE ec.entry_id=e.id AND ec.company_id=?))"
    if entry_id_raw:
        try: entry_id=int(entry_id_raw)
        except ValueError: return Response("Not found",status=404)
        rows=entry_rows(f"e.id=? AND {company_clause}",(entry_id,company_id)) if company_id is not None else entry_rows("e.id=?",(entry_id,))
        if not rows: return Response("Not found",status=404)
        filename=f"stiftungskalender-{rows[0]['day']}-{entry_id}.ics"; cal_name=f"{APP_TITLE} – {rows[0]['title']}"
    elif start_raw or end_raw:
        if company_id is not None: return Response("Not found",status=404)
        start_day=valid_iso_day(start_raw); end_day=valid_iso_day(end_raw)
        if not start_day or not end_day or start_day>end_day: return Response("Ungültiger Zeitraum",status=400)
        companies=[x.strip() for x in request.args.getlist("company") if x.strip()]; search=request.args.get("q","").strip(); rows=rows_for_calendar_range(start_day,end_day,companies,search)
        filename=f"stiftungskalender-{start_day}-bis-{end_day}.ics"; cal_name=ICAL_EXPORT_NAME
    else:
        rows=entry_rows(company_clause,(company_id,)) if company_id is not None else entry_rows()
    host=request.host.split(":")[0]; uid_mode="manual" if (entry_id_raw or start_raw or end_raw) else "feed"; body=build_ics_calendar(rows,host,cal_name,uid_mode=uid_mode); disposition="inline" if request.args.get("open")=="1" else "attachment"
    return Response(body,mimetype="text/calendar; charset=utf-8",headers={"Content-Disposition":f'{disposition}; filename="{filename}"'})

@app.get("/api/stats")
@login_required
def api_stats():
    year=request.args.get("year",str(date.today().year))
    with db() as conn:
        total=conn.execute("SELECT COUNT(*) AS c FROM entries WHERE day LIKE ?",(f"{year}-%",)).fetchone()["c"]
        rows=conn.execute("SELECT id,name,color FROM companies ORDER BY sort_order,name").fetchall(); per=[]
        for company in rows:
            count=conn.execute("""SELECT COUNT(*) AS c FROM entries e WHERE e.day LIKE ? AND (e.applies_to_all=1 OR EXISTS (SELECT 1 FROM entry_companies ec WHERE ec.entry_id=e.id AND ec.company_id=?))""",(f"{year}-%",company["id"])).fetchone()["c"]
            if count: per.append({"name":company["name"],"color":company["color"],"count":count})
    per.sort(key=lambda x:(-x["count"],x["name"]))
    return jsonify({"year":year,"total":total,"per_person":per,"per_company":per})


# Background calendar subscription synchronization (single gunicorn worker in the provided Dockerfile).
subscription_worker_started = threading.Thread(target=calendar_subscription_worker, name="calendar-subscriptions", daemon=True)
subscription_worker_started.start()
