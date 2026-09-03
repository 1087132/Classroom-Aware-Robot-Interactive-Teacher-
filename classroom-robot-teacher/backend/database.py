"""SQLite database layer for the Classroom-Aware Interactive Robot Teacher (CAT).

Stores lessons, sessions, students, responses, engagement samples and event logs
locally (privacy-by-design: no cloud storage, per the capstone design constraints).
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cat.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    subject     TEXT NOT NULL,
    data_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   TEXT NOT NULL REFERENCES lessons(id),
    status      TEXT NOT NULL DEFAULT 'idle',
    started_at  REAL,
    ended_at    REAL
);
CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    name        TEXT NOT NULL,
    seat_row    INTEGER NOT NULL,
    seat_col    INTEGER NOT NULL,
    joined_at   REAL NOT NULL,
    roster_id   INTEGER REFERENCES roster(id)
);
CREATE TABLE IF NOT EXISTS responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL,
    student_id   INTEGER NOT NULL,
    question_id  TEXT NOT NULL,
    answer_index INTEGER NOT NULL,
    is_correct   INTEGER NOT NULL,
    response_ms  INTEGER NOT NULL,
    points       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS engagement_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    student_id  INTEGER NOT NULL,
    ts          REAL NOT NULL,
    attention   REAL NOT NULL,
    participation REAL NOT NULL,
    quiz        REAL NOT NULL,
    speaking    REAL NOT NULL,
    score       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ask_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    student_id  INTEGER,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    on_topic    INTEGER NOT NULL,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    code        TEXT UNIQUE,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS teachers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    pw_salt     TEXT NOT NULL,
    pw_hash     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS speech_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    student_id  INTEGER,
    name        TEXT NOT NULL,
    transcript  TEXT NOT NULL,
    interaction INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade older databases: students.roster_id, roster.code (unique student IDs)."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(students)")]
    if "roster_id" not in cols:
        conn.execute("ALTER TABLE students ADD COLUMN roster_id INTEGER")

    # Legacy roster keyed by unique name -> rebuild without the name constraint.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='roster'").fetchone()
    if row and "UNIQUE COLLATE NOCASE" in (row["sql"] or ""):
        conn.execute("ALTER TABLE roster RENAME TO roster_old")
        conn.execute("""CREATE TABLE roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            code TEXT UNIQUE, created_at REAL NOT NULL)""")
        conn.execute("INSERT INTO roster (id, name, created_at)"
                     " SELECT id, name, created_at FROM roster_old")
        conn.execute("DROP TABLE roster_old")
    rcols = [r["name"] for r in conn.execute("PRAGMA table_info(roster)")]
    if "code" not in rcols:
        conn.execute("ALTER TABLE roster ADD COLUMN code TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roster_code ON roster(code)")

    # Backfill roster links for legacy session students (best effort, by name).
    for srow in conn.execute(
            "SELECT id, name FROM students WHERE roster_id IS NULL").fetchall():
        hit = conn.execute("SELECT id FROM roster WHERE name = ? COLLATE NOCASE",
                           (srow["name"].strip(),)).fetchone()
        rid = hit["id"] if hit else conn.execute(
            "INSERT INTO roster (name, created_at) VALUES (?, ?)",
            (srow["name"].strip(), time.time())).lastrowid
        conn.execute("UPDATE students SET roster_id=? WHERE id=?", (rid, srow["id"]))

    # Assign IDs to roster entries that predate the code column.
    for rrow in conn.execute("SELECT id FROM roster WHERE code IS NULL").fetchall():
        conn.execute("UPDATE roster SET code=? WHERE id=?",
                     (f"S-{rrow['id']:03d}", rrow["id"]))

    # Privacy: no personal names are stored - anonymize any legacy entries.
    conn.execute("UPDATE roster SET name = 'Student ' || id WHERE name NOT LIKE 'Student %'")
    conn.execute("""UPDATE students SET name = 'Student ' || roster_id
                    WHERE roster_id IS NOT NULL AND name NOT LIKE 'Student %'""")


def create_roster_entry() -> dict:
    """Register a NEW anonymous student: label 'Student N' + unique ID S-00N."""
    with get_conn() as conn:
        rid = conn.execute("INSERT INTO roster (name, created_at) VALUES ('', ?)",
                           (time.time(),)).lastrowid
        name, code = f"Student {rid}", f"S-{rid:03d}"
        conn.execute("UPDATE roster SET name=?, code=? WHERE id=?", (name, code, rid))
    return {"id": rid, "name": name, "code": code}


def get_roster_by_code(code: str) -> dict:
    """Exact lookup by unique student ID."""
    with get_conn() as conn:
        row = conn.execute("SELECT id, name, code FROM roster WHERE code = ?",
                           (code.strip().upper(),)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- teachers

def list_teachers() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id, username FROM teachers")]


def get_teacher(username: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM teachers WHERE username = ?",
                           ((username or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


def create_teacher(username: str, pw_salt: str, pw_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO teachers (username, pw_salt, pw_hash, created_at)"
                     " VALUES (?,?,?,?)",
                     (username.strip().lower(), pw_salt, pw_hash, time.time()))


def update_teacher_password(username: str, pw_salt: str, pw_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE teachers SET pw_salt=?, pw_hash=? WHERE username=?",
                     (pw_salt, pw_hash, username.strip().lower()))


def seed_lessons(lessons_dir: Path) -> None:
    """Load lesson script JSON files into the lessons table."""
    with get_conn() as conn:
        for f in sorted(lessons_dir.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT OR REPLACE INTO lessons (id, title, subject, data_json) VALUES (?,?,?,?)",
                (data["id"], data["title"], data["subject"], json.dumps(data)),
            )


def upsert_lesson(data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lessons (id, title, subject, data_json) VALUES (?,?,?,?)",
            (data["id"], data["title"], data["subject"], json.dumps(data)),
        )


def list_lessons() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, title, subject, data_json FROM lessons").fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data_json"])
        out.append({
            "id": r["id"], "title": r["title"], "subject": r["subject"],
            "grade": data.get("grade", ""),
            "num_questions": len(data.get("questions", [])),
            "duration_min": data.get("duration_min", 20),
        })
    return out


def get_lesson(lesson_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT data_json FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return json.loads(row["data_json"]) if row else None


def create_session(lesson_id: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (lesson_id, status, started_at) VALUES (?, 'greeting', ?)",
            (lesson_id, time.time()),
        )
        return cur.lastrowid


def update_session_status(session_id: int, status: str) -> None:
    with get_conn() as conn:
        if status == "ended":
            conn.execute("UPDATE sessions SET status=?, ended_at=? WHERE id=?",
                         (status, time.time(), session_id))
        else:
            conn.execute("UPDATE sessions SET status=? WHERE id=?", (status, session_id))


def add_student(session_id: int, name: str, row: int, col: int, roster_id=None) -> int:
    if roster_id is None:
        entry = create_roster_entry()
        roster_id, name = entry["id"], entry["name"]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO students (session_id, name, seat_row, seat_col, joined_at, roster_id)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, name, row, col, time.time(), roster_id),
        )
        return cur.lastrowid


def get_students(session_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, seat_row, seat_col, roster_id FROM students WHERE session_id=?",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_response(session_id: int, student_id: int, question_id: str,
                 answer_index: int, is_correct: bool, response_ms: int, points: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO responses (session_id, student_id, question_id, answer_index,"
            " is_correct, response_ms, points, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, student_id, question_id, answer_index, int(is_correct),
             response_ms, points, time.time()),
        )


def get_responses(session_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM responses WHERE session_id=?", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def add_engagement_sample(session_id: int, student_id: int, a: float, p: float,
                          q: float, s: float, score: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO engagement_samples (session_id, student_id, ts, attention,"
            " participation, quiz, speaking, score) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, student_id, time.time(), a, p, q, s, score),
        )


def get_engagement_samples(session_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM engagement_samples WHERE session_id=? ORDER BY ts", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def log_event(session_id, type_: str, detail: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO events (session_id, ts, type, detail) VALUES (?,?,?,?)",
                     (session_id, time.time(), type_, detail))


def get_events(session_id: int, limit: int = 200) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, type, detail FROM events WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def log_ask(session_id, student_id, question: str, answer: str, on_topic: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ask_log (session_id, student_id, question, answer, on_topic, ts)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, student_id, question, answer, int(on_topic), time.time()),
        )


def get_ask_log(session_id: int, limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ask_log WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_speech(session_id, student_id, name: str, transcript: str,
               interaction: bool, kind: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO speech_log (session_id, student_id, name, transcript,"
            " interaction, kind, ts) VALUES (?,?,?,?,?,?,?)",
            (session_id, student_id, name, transcript, int(interaction), kind, time.time()),
        )


def get_speech_log(session_id: int, limit: int = 60) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM speech_log WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- tracking history

def history_sessions() -> list:
    """Past sessions with lesson info, participation counts and class averages."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.id, s.lesson_id, s.status, s.started_at, s.ended_at,
                   l.title AS lesson_title, l.subject,
                   (SELECT COUNT(*) FROM students st WHERE st.session_id = s.id) AS num_students,
                   (SELECT ROUND(AVG(score), 1) FROM engagement_samples e
                     WHERE e.session_id = s.id) AS class_avg,
                   (SELECT COUNT(*) FROM responses r WHERE r.session_id = s.id) AS num_responses
            FROM sessions s JOIN lessons l ON l.id = s.lesson_id
            ORDER BY s.started_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def history_session_detail(session_id: int) -> dict:
    """Per-student averages + class timeline for one past session."""
    with get_conn() as conn:
        head = conn.execute("""
            SELECT s.id, s.lesson_id, s.status, s.started_at, s.ended_at,
                   l.title AS lesson_title, l.subject
            FROM sessions s JOIN lessons l ON l.id = s.lesson_id WHERE s.id = ?
        """, (session_id,)).fetchone()
        if not head:
            return None
        students = conn.execute("""
            SELECT st.id, st.name, st.roster_id, st.seat_row, st.seat_col,
                   ro.code AS code,
                   ROUND(AVG(e.attention), 1)     AS attention,
                   ROUND(AVG(e.participation), 1) AS participation,
                   ROUND(AVG(e.quiz), 1)          AS quiz,
                   ROUND(AVG(e.speaking), 1)      AS speaking,
                   ROUND(AVG(e.score), 1)         AS score,
                   (SELECT COUNT(*) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS answered,
                   (SELECT COALESCE(SUM(r.is_correct), 0) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS correct,
                   (SELECT COALESCE(SUM(r.points), 0) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS points
            FROM students st
            LEFT JOIN roster ro ON ro.id = st.roster_id
            LEFT JOIN engagement_samples e
                   ON e.session_id = st.session_id AND e.student_id = st.id
            WHERE st.session_id = ?
            GROUP BY st.id ORDER BY score DESC
        """, (session_id,)).fetchall()
        timeline = conn.execute("""
            SELECT CAST(ts / 5 AS INTEGER) * 5 AS bucket,
                   ROUND(AVG(score), 1) AS class_score,
                   ROUND(AVG(attention), 1) AS class_attention
            FROM engagement_samples WHERE session_id = ?
            GROUP BY bucket ORDER BY bucket
        """, (session_id,)).fetchall()
        asks = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(on_topic), 0) AS on_topic"
            " FROM ask_log WHERE session_id = ?", (session_id,)).fetchone()
    scores = [r["score"] for r in students if r["score"] is not None]
    return {
        "session": dict(head),
        "students": [dict(r) for r in students],
        "class_average": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "total_responses": sum(r["answered"] for r in students),
        "questions_to_robot": asks["n"],
        "on_topic_answered": asks["on_topic"],
        "off_topic_refused": asks["n"] - asks["on_topic"],
        "timeline": [{"ts": r["bucket"], "class_score": r["class_score"],
                      "class_attention": r["class_attention"]} for r in timeline],
    }


def history_students() -> list:
    """Roster registry with attendance and cross-session averages."""
    with get_conn() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        rows = conn.execute("""
            SELECT ro.id AS roster_id, ro.name, ro.code,
                   COUNT(DISTINCT st.session_id) AS sessions_attended,
                   MAX(st.joined_at) AS last_seen,
                   (SELECT ROUND(AVG(e.score), 1) FROM engagement_samples e
                     JOIN students st2 ON st2.id = e.student_id AND st2.session_id = e.session_id
                     WHERE st2.roster_id = ro.id) AS avg_engagement,
                   (SELECT ROUND(AVG(r.is_correct) * 100, 1) FROM responses r
                     JOIN students st3 ON st3.id = r.student_id AND st3.session_id = r.session_id
                     WHERE st3.roster_id = ro.id) AS quiz_accuracy,
                   (SELECT COALESCE(SUM(r.points), 0) FROM responses r
                     JOIN students st4 ON st4.id = r.student_id AND st4.session_id = r.session_id
                     WHERE st4.roster_id = ro.id) AS total_points
            FROM roster ro LEFT JOIN students st ON st.roster_id = ro.id
            GROUP BY ro.id ORDER BY ro.name COLLATE NOCASE
        """).fetchall()
    out = []
    with get_conn() as conn:
        for r in rows:
            d = dict(r)
            d["total_sessions"] = total_sessions
            d["attendance_pct"] = round(d["sessions_attended"] / total_sessions * 100, 1) if total_sessions else 0.0
            per_session = conn.execute("""
                SELECT ROUND(AVG(e.score), 1) AS score
                FROM students st
                JOIN sessions s ON s.id = st.session_id
                LEFT JOIN engagement_samples e
                       ON e.session_id = st.session_id AND e.student_id = st.id
                WHERE st.roster_id = ?
                GROUP BY st.id ORDER BY s.started_at
            """, (d["roster_id"],)).fetchall()
            vals = [p["score"] for p in per_session if p["score"] is not None]
            d["trend"] = round(vals[-1] - vals[-2], 1) if len(vals) >= 2 else None
            out.append(d)
    return out


def history_student_detail(roster_id: int) -> dict:
    """One roster student's per-session breakdown for trend charts."""
    with get_conn() as conn:
        head = conn.execute("SELECT id, name, code, created_at FROM roster WHERE id = ?",
                            (roster_id,)).fetchone()
        if not head:
            return None
        sessions = conn.execute("""
            SELECT s.id AS session_id, s.started_at, l.title AS lesson_title, l.subject,
                   ROUND(AVG(e.attention), 1)     AS attention,
                   ROUND(AVG(e.participation), 1) AS participation,
                   ROUND(AVG(e.quiz), 1)          AS quiz,
                   ROUND(AVG(e.speaking), 1)      AS speaking,
                   ROUND(AVG(e.score), 1)         AS score,
                   (SELECT COUNT(*) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS answered,
                   (SELECT COALESCE(SUM(r.is_correct), 0) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS correct,
                   (SELECT COALESCE(SUM(r.points), 0) FROM responses r
                     WHERE r.session_id = st.session_id AND r.student_id = st.id) AS points
            FROM students st
            JOIN sessions s ON s.id = st.session_id
            JOIN lessons l ON l.id = s.lesson_id
            LEFT JOIN engagement_samples e
                   ON e.session_id = st.session_id AND e.student_id = st.id
            WHERE st.roster_id = ?
            GROUP BY st.id ORDER BY s.started_at
        """, (roster_id,)).fetchall()
        asks = conn.execute("""
            SELECT COUNT(*) AS n FROM ask_log a
            JOIN students st ON st.id = a.student_id AND st.session_id = a.session_id
            WHERE st.roster_id = ?
        """, (roster_id,)).fetchone()["n"]
    return {
        "roster_id": head["id"], "name": head["name"], "code": head["code"],
        "created_at": head["created_at"],
        "sessions": [dict(r) for r in sessions],
        "questions_to_robot": asks,
    }
