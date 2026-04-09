import os
import re
from datetime import datetime, date, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---- Config
DATABASE_URL = os.environ["DATABASE_URL"]
OUTGOING_WEBHOOK_TOKEN = os.environ.get("OUTGOING_WEBHOOK_TOKEN")
TZ = ZoneInfo(os.environ.get("HOLIDAYBOT_TZ", "Asia/Kolkata"))

DATE_FORMATS = ["%d%b%y", "%d%b%Y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"]

ADD_RE = re.compile(
    r"""^\s*add\s+leave\s+from:(?P<from>\S+)\s+to:(?P<to>\S+)(?:\s+reason:(?P<reason>.+))?\s*$""",
    re.IGNORECASE,
)
REMOVE_RE = re.compile(
    r"""^\s*remove\s+leave\s+from:(?P<from>\S+)\s+to:(?P<to>\S+)(?:\s+reason:(?P<reason>.+))?\s*$""",
    re.IGNORECASE,
)
SHOW_RE = re.compile(
    r"""^\s*show\s+leave(?:\s+week:(?P<week>\S+))?\s*$""",
    re.IGNORECASE,
)
MONTH_RE = re.compile(
    r"""^\s*show\s+leave(?:\s+month:(?P<month>\S+))(?:\s+to:(?P<to>\S+))?\s*$""",
    re.IGNORECASE,
)
MENTION_RE = re.compile(r"^@\*\*[^*]+\*\*\s*", re.UNICODE)


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date '{s}'. Try 14Jan26 or 2026-01-14.")


def clean_reason(r: str | None) -> str:
    if not r:
        return ""
    r = r.strip()
    if (r.startswith('"') and r.endswith('"')) or (r.startswith("'") and r.endswith("'")):
        r = r[1:-1].strip()
    return r


def week_range(anchor: date):
    start = anchor - timedelta(days=anchor.weekday())  # Mon
    end = start + timedelta(days=6)  # Sun
    return start, end


def parse_week(sel: str | None):
    today = datetime.now(TZ).date()
    if not sel or sel.lower() in ("this", "current"):
        ws, we = week_range(today)
        return ws, we, "this week"
    if sel.lower() == "next":
        ws, we = week_range(today + timedelta(days=7))
        return ws, we, "next week"

    if re.search(r"[A-Za-z]", sel):
        anchor = parse_date(sel)
    else:
        anchor = datetime.strptime(sel, "%Y-%m-%d").date()

    ws, we = week_range(anchor)
    return ws, we, f"week of {ws}"


def month_range(sel: str):
    today = datetime.now(TZ).date()
    s = sel.strip().lower()

    if s in ("this", "current"):
        y, m = today.year, today.month
    elif s == "next":
        if today.month == 12:
            y, m = today.year + 1, 1
        else:
            y, m = today.year, today.month + 1
    else:
        y, m = map(int, sel.split("-"))
        if not (1 <= m <= 12):
            raise ValueError("Month must be in YYYY-MM format.")

    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    label = "this month" if s in ("this", "current") else "next month" if s == "next" else f"{y:04d}-{m:02d}"
    return start, end, label


def ensure_schema(con):
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


def usage():
    return (
        "Commands:\n"
        "- add leave from:14Jan26 to:16Jan26 reason:\"study leave\"\n"
        "- remove leave from:14Jan26 to:16Jan26 reason:\"change in plan\"\n"
        "- show leave\n"
        "- show leave week:this | week:next | week:2026-01-14\n"
        "- show leave month:this | month:next | month:2026-01\n"
        "- show leave month:2026-01 to:2026-02\n"
    )


@app.get("/api/holidaybot")
def health():
    return {"ok": True, "message": "holidaybot up"}


@app.post("/api/holidaybot")
async def holidaybot(req: Request):
    payload = await req.json()

    # Verify token
    if OUTGOING_WEBHOOK_TOKEN and payload.get("token") != OUTGOING_WEBHOOK_TOKEN:
        return JSONResponse({"response_not_required": True})

    msg = payload.get("message") or {}
    content = (payload.get("data") or msg.get("content") or "").strip()
    content = MENTION_RE.sub("", content).strip()

    sender = msg.get("sender_full_name") or "Unknown"
    sender_id = int(msg.get("sender_id") or 0)

    if not content:
        return {"content": usage()}

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
                    (sender_id, sender, start, end, reason),
                ).fetchone()

                leave_id = row[0]
                return {
                    "content": f"✅ Added leave #{leave_id} for {sender}: {start} → {end}"
                    + (f' (reason: "{reason}")' if reason else "")
                }
            except Exception as e:
                return {"content": f"❌ Could not add leave: {e}\n\n{usage()}"}

        m = REMOVE_RE.match(content)
        if m:
            try:
                rem_start = parse_date(m.group("from"))
                rem_end = parse_date(m.group("to"))
                _reason = clean_reason(m.group("reason"))
                if rem_end < rem_start:
                    raise ValueError("End date is before start date.")

                rows = con.execute(
                    """
                    SELECT id, start_date, end_date, reason
                    FROM leaves
                    WHERE user_id = %s
                      AND NOT (end_date < %s OR start_date > %s)
                    ORDER BY start_date ASC, id ASC
                    """,
                    (sender_id, rem_start, rem_end),
                ).fetchall()

                if not rows:
                    return {
                        "content": f"No leave found for {sender} overlapping {rem_start} -> {rem_end}."
                    }

                deleted = 0
                updated = 0
                inserted = 0

                for leave_id, start, end, reason in rows:
                    if rem_start <= start and rem_end >= end:
                        con.execute("DELETE FROM leaves WHERE id = %s", (leave_id,))
                        deleted += 1
                        continue

                    if rem_start <= start <= rem_end < end:
                        new_start = rem_end + timedelta(days=1)
                        con.execute(
                            "UPDATE leaves SET start_date = %s WHERE id = %s",
                            (new_start, leave_id),
                        )
                        updated += 1
                        continue

                    if start < rem_start <= end <= rem_end:
                        new_end = rem_start - timedelta(days=1)
                        con.execute(
                            "UPDATE leaves SET end_date = %s WHERE id = %s",
                            (new_end, leave_id),
                        )
                        updated += 1
                        continue

                    if start < rem_start and end > rem_end:
                        left_end = rem_start - timedelta(days=1)
                        right_start = rem_end + timedelta(days=1)
                        con.execute(
                            "UPDATE leaves SET end_date = %s WHERE id = %s",
                            (left_end, leave_id),
                        )
                        con.execute(
                            """
                            INSERT INTO leaves (user_id, user_name, start_date, end_date, reason)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (sender_id, sender, right_start, end, reason),
                        )
                        updated += 1
                        inserted += 1
                        continue

                return {
                    "content": (
                        f"Removed leave for {sender} in {rem_start} -> {rem_end}. "
                        f"Deleted: {deleted}, updated: {updated}, split: {inserted}."
                    )
                }
            except Exception as e:
                return {"content": f"Could not remove leave: {e}\n\n{usage()}"}

        m = SHOW_RE.match(content)
        if m:
            try:
                ws, we, label = parse_week(m.group("week"))
                rows = con.execute(
                    """
                    SELECT user_name, start_date, end_date, reason
                    FROM leaves
                    WHERE NOT (end_date < %s OR start_date > %s)
                    ORDER BY start_date ASC, user_name ASC
                    """,
                    (ws, we),
                ).fetchall()

                if not rows:
                    return {"content": f"No leave recorded for {label} ({ws} to {we})."}

                lines = [f"Leave for {label} ({ws} to {we}):"]
                for u, s, e, r in rows:
                    lines.append(f"- {u}: {s} → {e}" + (f' — "{r}"' if r else ""))
                return {"content": "\n".join(lines)}
            except Exception as e:
                return {"content": f"❌ Could not show leave: {e}\n\n{usage()}"}

        m = MONTH_RE.match(content)
        if m:
            try:
                start, end, label = month_range(m.group("month"))
                if m.group("to"):
                    end_start, end_end, end_label = month_range(m.group("to"))
                    start = min(start, end_start)
                    end = max(end, end_end)
                    label = f"{label} to {end_label}"

                rows = con.execute(
                    """
                    SELECT user_name, start_date, end_date, reason
                    FROM leaves
                    WHERE NOT (end_date < %s OR start_date > %s)
                    ORDER BY start_date ASC, user_name ASC
                    """,
                    (start, end),
                ).fetchall()

                if not rows:
                    return {"content": f"No leave recorded for {label} ({start} to {end})."}

                lines = [f"Leave for {label} ({start} to {end}):"]
                for u, s, e, r in rows:
                    lines.append(f"- {u}: {s} → {e}" + (f' — "{r}"' if r else ""))
                return {"content": "\n".join(lines)}
            except Exception as e:
                return {"content": f"❌ Could not show month leave: {e}\n\n{usage()}"}

    return {"content": f"Sorry, I didn't understand.\n\n{usage()}"}
