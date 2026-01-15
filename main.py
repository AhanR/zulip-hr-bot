import json
import os
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler

import psycopg

# -----------------------
# Config
# -----------------------
TZ = ZoneInfo(os.environ.get("HOLIDAYBOT_TZ", "Asia/Kolkata"))

DATABASE_URL = os.environ.get("DATABASE_URL")  # Neon/Vercel Marketplace Postgres URL
OUTGOING_WEBHOOK_TOKEN = os.environ.get("OUTGOING_WEBHOOK_TOKEN")  # verify incoming webhook calls

DATE_FORMATS = [
    "%d%b%y",      # 14Jan26
    "%d%b%Y",      # 14Jan2026
    "%Y-%m-%d",    # 2026-01-14
    "%d/%m/%Y",    # 14/01/2026
    "%d/%m/%y",    # 14/01/26
]

ADD_RE = re.compile(
    r"""^\s*add\s+leave\s+from:(?P<from>\S+)\s+to:(?P<to>\S+)(?:\s+reason:(?P<reason>.+))?\s*$""",
    re.IGNORECASE,
)
SHOW_RE = re.compile(
    r"""^\s*show\s+leave(?:\s+week:(?P<week>\S+))?\s*$""",
    re.IGNORECASE,
)

MENTION_RE = re.compile(r"^@\*\*[^*]+\*\*\s*", re.UNICODE)  # strip leading @**Bot Name**


# -----------------------
# Helpers
# -----------------------
def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date '{s}'. Try 14Jan26 or 2026-01-14.")


def clean_reason(reason_raw: str | None) -> str:
    if not reason_raw:
        return ""
    r = reason_raw.strip()
    if (r.startswith('"') and r.endswith('"')) or (r.startswith("'") and r.endswith("'")):
        r = r[1:-1].strip()
    return r


def week_range(anchor: date) -> tuple[date, date]:
    # Monday..Sunday
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=6)
    return start, end


def parse_week_selector(sel: str | None) -> tuple[date, date, str]:
    today = datetime.now(TZ).date()

    if not sel or sel.lower() in ("this", "current"):
        ws, we = week_range(today)
        return ws, we, "this week"

    if sel.lower() == "next":
        ws, we = week_range(today + timedelta(days=7))
        return ws, we, "next week"

    # allow week:2026-01-14 or week:14Jan26
    try:
        anchor = parse_date(sel) if re.search(r"[A-Za-z]", sel) else datetime.strptime(sel, "%Y-%m-%d").date()
    except Exception:
        raise ValueError("week: must be one of this|next|YYYY-MM-DD|14Jan26")

    ws, we = week_range(anchor)
    return ws, we, f"week of {ws.isoformat()}"


def ensure_schema(con: psycopg.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS leaves (
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          user_name TEXT NOT NULL,
          start_date DATE NOT NULL,
          end_date DATE NOT NULL,
          reason TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_leaves_dates ON leaves (start_date, end_date);")


def usage() -> str:
    return (
        "Commands:\n"
        "• add leave from:14Jan26 to:16Jan26 reason:\"study leave\"\n"
        "• show leave\n"
        "• show leave week:this | week:next | week:2026-01-14\n"
    )


def format_rows(rows: list[tuple], week_start: date, week_end: date, label: str) -> str:
    if not rows:
        return f"No leave recorded for {label} ({week_start} to {week_end})."

    lines = [f"Leave for {label} ({week_start} to {week_end}):"]
    for user_name, s, e, reason in rows:
        r = f' — "{reason}"' if reason else ""
        lines.append(f"- {user_name}: {s} → {e}{r}")
    return "\n".join(lines)


# -----------------------
# Vercel Function handler
# -----------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Useful for testing the endpoint in browser.
        json_response(self, 200, {"ok": True, "message": "holidaybot endpoint up"})

    def do_POST(self):
        try:
            if not DATABASE_URL:
                return json_response(self, 500, {"content": "❌ Server misconfigured: DATABASE_URL is missing."})

            raw = self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except Exception:
                return json_response(self, 400, {"content": "❌ Invalid JSON payload."})

            # Auth: verify the outgoing webhook token if configured
            incoming_token = payload.get("token")
            if OUTGOING_WEBHOOK_TOKEN and incoming_token != OUTGOING_WEBHOOK_TOKEN:
                return json_response(self, 403, {"response_not_required": True})

            # Zulip native format includes: payload["data"] (raw markdown), payload["message"] (dict)
            msg = payload.get("message") or {}
            content = (payload.get("data") or msg.get("content") or "").strip()
            content = MENTION_RE.sub("", content).strip()  # remove leading "@**Bot**" mention if present

            sender_name = msg.get("sender_full_name") or "Unknown"
            sender_id = int(msg.get("sender_id") or 0)

            if not content:
                return json_response(self, 200, {"content": f"Sorry, I didn’t get a command.\n\n{usage()}"})

            # DB work
            with psycopg.connect(DATABASE_URL) as con:
                ensure_schema(con)

                m = ADD_RE.match(content)
                if m:
                    try:
                        start = parse_date(m.group("from"))
                        end = parse_date(m.group("to"))
                        reason = clean_reason(m.group("reason"))
                        if end < start:
                            raise ValueError("End date is before start date.")

                        row = con.execute(
                            """
                            INSERT INTO leaves (user_id, user_name, start_date, end_date, reason)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (sender_id, sender_name, start, end, reason),
                        ).fetchone()

                        leave_id = row[0] if row else "?"
                        return json_response(
                            self,
                            200,
                            {
                                "content": (
                                    f"✅ Added leave #{leave_id} for {sender_name}: {start} → {end}"
                                    + (f' (reason: "{reason}")' if reason else "")
                                )
                            },
                        )
                    except Exception as e:
                        return json_response(self, 200, {"content": f"❌ Could not add leave: {e}\n\n{usage()}"})

                m = SHOW_RE.match(content)
                if m:
                    try:
                        week_sel = m.group("week")
                        ws, we, label = parse_week_selector(week_sel)

                        rows = con.execute(
                            """
                            SELECT user_name, start_date, end_date, reason
                            FROM leaves
                            WHERE NOT (end_date < %s OR start_date > %s)
                            ORDER BY start_date ASC, user_name ASC
                            """,
                            (ws, we),
                        ).fetchall()

                        return json_response(self, 200, {"content": format_rows(rows, ws, we, label)})
                    except Exception as e:
                        return json_response(self, 200, {"content": f"❌ Could not show leave: {e}\n\n{usage()}"})

            return json_response(self, 200, {"content": f"Sorry, I didn’t understand.\n\n{usage()}"})

        except Exception:
            # Avoid leaking internals to chat; still return valid webhook response.
            return json_response(self, 200, {"content": "❌ Something went wrong on the server."})