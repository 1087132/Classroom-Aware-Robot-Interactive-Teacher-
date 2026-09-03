"""Classroom-Aware Interactive Robot Teacher (CAT) - local server.

Runs the whole prototype on the local network (no cloud), per the capstone
design constraints:

  - Teacher dashboard   http://<this-machine>:8000/teacher
  - Student response    http://<lan-ip>:8000/student   (QR code provided)
  - Robot face          http://<this-machine>:8000/robot
  - Projector view      http://<this-machine>:8000/projector

REST APIs handle commands; WebSockets push real-time events (engagement
updates every 5 s, questions, robot expressions/speech).
"""
import asyncio
import json
import os
import random
import re
import shutil
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import auth
from . import database as db
from .analytics import Analytics
from .lesson_engine import LessonEngine
from .llm import ConstrainedLLM
from .perception import Perception

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PORT = 8000

engine = LessonEngine()
perception = Perception()
analytics = Analytics()
llm = ConstrainedLLM()

robot_expression = "neutral"
_minigame_next_at = None

# Robot voice through the server host's speaker, queued sequentially.
# In Docker/Linux there is no host audio device -> browser voice takes over
# (the robot-face page fetches the same neural voice via /api/tts).
HAS_HOST_AUDIO = bool(shutil.which("afplay") or shutil.which("say"))
server_speaker = HAS_HOST_AUDIO
_speak_queue = None          # created in lifespan


async def _speaker_worker():
    from . import tts
    while True:
        text = await _speak_queue.get()
        try:
            wav = await asyncio.to_thread(tts.synth_to_wav, text[:400])
            if wav:                                   # natural neural voice (offline)
                proc = await asyncio.create_subprocess_exec(
                    "afplay", wav,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                Path(wav).unlink(missing_ok=True)
            else:                                     # fallback: macOS built-in voice
                proc = await asyncio.create_subprocess_exec(
                    "say", "-r", "175", text[:400],
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
        except Exception as e:
            print("speaker error:", e)


def queue_speech(text: str):
    """Send robot speech to the computer's speaker (drops backlog beyond 5 lines)."""
    if not server_speaker or not HAS_HOST_AUDIO or not text or _speak_queue is None:
        return
    if _speak_queue.qsize() >= 5:
        try:
            _speak_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    _speak_queue.put_nowait(text)


# Automated face control driven by class engagement (teacher can toggle/override)
auto_face = True
_last_expr_ts = 0.0         # last time ANY expression event was emitted
_expr_hold_until = 0.0      # manual override holds auto for a while
_last_nudge_ts = 0.0

# Lesson scheduling + automatic pacing
schedules = {}              # id -> {lesson_id, due_ts, auto_advance, minigame_after_min, status}
_schedule_seq = 0
auto_advance = False        # robot paces greeting/content/review automatically
_aa_key = None              # (status, flow_i, queue len) to detect stage changes
_aa_since = 0.0
_minigame_at_ts = None      # scheduled mini-game moment within the lesson
AA_DURATIONS = {"greeting": 10, "content": 35, "review": 10}


def lan_ip() -> str:
    # In Docker the detected IP is the container's - set CAT_PUBLIC_HOST to the
    # host machine's LAN IP so QR codes / student URLs work on other devices.
    override = os.environ.get("CAT_PUBLIC_HOST")
    if override:
        return override
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


# ---------------------------------------------------------------- websockets
class WSManager:
    def __init__(self):
        self.clients = []   # (websocket, role)

    async def connect(self, ws: WebSocket, role: str):
        await ws.accept()
        self.clients.append((ws, role))

    def disconnect(self, ws: WebSocket):
        self.clients = [(w, r) for (w, r) in self.clients if w is not ws]

    async def broadcast(self, event: dict):
        dead = []
        text = json.dumps(event)
        for ws, _role in self.clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def counts(self) -> dict:
        out = {}
        for _w, r in self.clients:
            out[r] = out.get(r, 0) + 1
        return out


manager = WSManager()


async def emit(events):
    """Broadcast a list of engine events; track expression; voice via speaker."""
    global robot_expression, _last_expr_ts
    for ev in events:
        if ev.get("type") == "expression":
            robot_expression = ev["expression"]
            _last_expr_ts = time.time()
        elif ev.get("type") == "speak":
            queue_speech(ev.get("text", ""))
        await manager.broadcast(ev)


# ---------------------------------------------------------------- robot brain
NUDGE_LOW = [
    "Hmm, I sense some wandering minds! Eyes on me, superstars!",
    "Let's wake up our brains! Sit tall and focus!",
    "I miss your bright eyes! Let's get back on track together!",
]
NUDGE_HIGH = [
    "Wow, this class is on fire today! Keep it up!",
    "Amazing focus, everyone! You make my circuits happy!",
]


def face_for_score(score: float) -> str:
    """Map class engagement score to a baseline facial expression."""
    if score >= 80:
        return "happy"
    if score >= 60:
        return "neutral"
    if score >= 45:
        return "thinking"
    return "concerned"


async def auto_face_update(class_score: float):
    """Engagement-driven face: adjusts the robot's expression every few seconds,
    without stomping on fresh lesson events or a teacher's manual override."""
    global _last_nudge_ts
    now = time.time()
    if not auto_face or not engine.active:
        return
    if engine.status in ("greeting", "ended", "paused", "idle"):
        return
    if now < _expr_hold_until or now - _last_expr_ts < 12:
        return
    target = face_for_score(class_score)
    if target != robot_expression:
        await emit([{"type": "expression", "expression": target}])
        await manager.broadcast({"type": "auto_face", "score": class_score, "expression": target})
    if now - _last_nudge_ts > 90:
        if class_score < 45:
            _last_nudge_ts = now
            await emit([{"type": "speak", "text": random.choice(NUDGE_LOW)}])
            db.log_event(engine.session_id, "auto_face",
                         f"Low engagement nudge at score {class_score:.0f}")
        elif class_score >= 85:
            _last_nudge_ts = now
            await emit([{"type": "speak", "text": random.choice(NUDGE_HIGH)}])
            db.log_event(engine.session_id, "auto_face",
                         f"Praise at score {class_score:.0f}")


async def send_answer_feedback(student_id, name, question, answer_index, correct):
    """LLM feedback on a student's MCQ answer - generated async, pushed over WS."""
    text = await asyncio.to_thread(
        llm.answer_feedback, question["text"], question["options"], answer_index,
        question["answer"], question.get("explanation", ""), correct)
    if text:
        await manager.broadcast({"type": "answer_feedback", "student_id": student_id,
                                 "question_id": question["id"], "text": text})
        db.log_event(engine.session_id, "llm_feedback", f"{name}: {text[:80]}")


async def send_review_insight(results):
    """LLM classroom insight on the closed MCQ's answer distribution."""
    text = await asyncio.to_thread(
        llm.review_comment, results["text"], results["options"], results["distribution"],
        results["correct_index"], results["correct"], results["answers"])
    if text and engine.active:
        db.log_event(engine.session_id, "llm_insight", text[:100])
        await emit([{"type": "speak", "text": text}])


async def call_on_student(st: dict):
    """Teacher-triggered: the robot calls on a student by number."""
    perception.boost(st["id"], 15)
    perception.record_speaking(st["id"])
    db.log_event(engine.session_id, "call_on", f"Robot called on {st['name']}")
    await emit([
        {"type": "expression", "expression": "curious"},
        {"type": "speak", "text": f"{st['name']}, I'd love to hear your thoughts! "
                                  f"What do you think about what we just learned?"},
        {"type": "toast", "level": "info", "text": f"Robot called on {st['name']}"},
    ])


async def push_easy_question(student: dict):
    asked = getattr(engine, "asked_qids", set())
    qs = engine.lesson.get("questions", [])
    pick = (next((q for q in qs if q.get("difficulty") == "easy" and q["id"] not in asked), None)
            or next((q for q in qs if q["id"] not in asked), None) or (qs[0] if qs else None))
    if not pick:
        return
    perception.boost(student["id"], 10)
    full_student = {"id": student["id"], "name": student["name"]}
    await emit(engine.open_question(pick["id"], target_student=full_student))


# ---------------------------------------------------------------- lessons: validate/upload
LESSONS_DIR = Path(__file__).parent / "lessons"


def validate_lesson(data):
    """Validate an uploaded lesson; fill sensible defaults. Returns (errors, lesson)."""
    if not isinstance(data, dict):
        return ["Lesson must be a JSON object."], None
    errors = []
    lid = re.sub(r"[^a-z0-9_-]", "", str(data.get("id", "")).strip().lower().replace(" ", "_"))
    if not lid:
        errors.append("Missing or invalid 'id' (use letters/numbers/underscores).")
    for field in ("title", "subject"):
        if not str(data.get(field, "")).strip():
            errors.append(f"Missing '{field}'.")
    questions = data.get("questions") or []
    if not questions:
        errors.append("'questions' must contain at least one question.")
    qids = set()
    for i, q in enumerate(questions):
        tag = f"questions[{i}]"
        if not q.get("id"):
            errors.append(f"{tag}: missing 'id'.")
        elif q["id"] in qids:
            errors.append(f"{tag}: duplicate id '{q['id']}'.")
        else:
            qids.add(q["id"])
        if not str(q.get("text", "")).strip():
            errors.append(f"{tag}: missing 'text'.")
        opts = q.get("options") or []
        if len(opts) < 2 or len(opts) > 4:
            errors.append(f"{tag}: needs 2-4 'options'.")
        if not isinstance(q.get("answer"), int) or not (0 <= q.get("answer", -1) < len(opts)):
            errors.append(f"{tag}: 'answer' must be the index of the correct option.")
        q.setdefault("time_limit", 30)
        q.setdefault("difficulty", "easy")
        q.setdefault("explanation", "")
        q.setdefault("keywords", [])
    mg = data.get("minigame")
    mg_ids = set(mg.get("question_ids", [])) if mg else set()
    if mg:
        missing = mg_ids - qids
        if missing:
            errors.append(f"minigame.question_ids not found in questions: {sorted(missing)}")
    if errors:
        return errors, None
    data["id"] = lid
    data.setdefault("grade", "")
    data.setdefault("topic_label", data["title"])
    data.setdefault("duration_min", 20)
    data.setdefault("greeting", f"Hello class! I am CAT. Today's lesson is {data['title']}. Let's begin!")
    data.setdefault("closing", "That's all for today. Great job, everyone!")
    data.setdefault("knowledge_base", [])
    if not data.get("flow"):
        data["flow"] = [{"type": "question", "question_id": q["id"]}
                        for q in questions if q["id"] not in mg_ids]
    return [], data


LESSON_TEMPLATE = {
    "id": "my_lesson",
    "title": "My New Lesson",
    "subject": "Science",
    "grade": "Grade 5-6",
    "topic_label": "my topic",
    "duration_min": 20,
    "greeting": "Hello class! Today we learn about my topic!",
    "closing": "Great work everyone, see you next time!",
    "flow": [
        {"type": "content", "title": "Introduction", "body": "Explain the topic here.",
         "speak": "What the robot says for this slide."},
        {"type": "question", "question_id": "q1"},
    ],
    "questions": [
        {"id": "q1", "text": "Example question?", "options": ["Option A", "Option B", "Option C"],
         "answer": 0, "time_limit": 30, "difficulty": "easy",
         "explanation": "Why option A is correct.", "keywords": ["example"]},
        {"id": "g1", "text": "Quick bonus question?", "options": ["Yes", "No"],
         "answer": 0, "time_limit": 10, "difficulty": "easy",
         "explanation": "", "keywords": []},
    ],
    "minigame": {"title": "Quick-Fire Round",
                 "intro": "Bonus round! Double points!", "question_ids": ["g1"]},
    "knowledge_base": [
        {"q": "What is my topic?", "a": "The robot answers student questions from these facts.",
         "keywords": ["topic"]},
    ],
}


# ---------------------------------------------------------------- session start helper
async def start_lesson_internal(lesson_id: str, with_auto_advance=False, minigame_after_min=None):
    global auto_advance, _minigame_at_ts, _aa_key, _aa_since
    lesson = db.get_lesson(lesson_id)
    if not lesson:
        return False
    analytics.reset()
    perception.reset_students()
    llm.load_lesson(lesson)
    events = engine.start(lesson)
    engine.asked_qids = set()
    auto_advance = bool(with_auto_advance)
    _aa_key, _aa_since = None, time.time()
    _minigame_at_ts = (time.time() + float(minigame_after_min) * 60
                       if minigame_after_min and lesson.get("minigame") else None)
    await manager.broadcast({"type": "state", **engine.public_state()})
    await manager.broadcast({"type": "autoadvance_state", "enabled": auto_advance})
    await emit(events)
    return True


# ---------------------------------------------------------------- tick loop
async def tick_loop():
    global _minigame_next_at, _aa_key, _aa_since, _minigame_at_ts, auto_advance
    last_analytics = 0.0
    while True:
        try:
            await asyncio.sleep(1.0)
            now = time.time()

            # ---- scheduled lesson starts (runs even while idle) ----
            for sch in schedules.values():
                if sch["status"] != "pending" or now < sch["due_ts"]:
                    continue
                if engine.active:
                    sch["status"] = "missed"
                    await manager.broadcast({"type": "toast", "level": "warn",
                                             "text": f"Scheduled lesson '{sch['lesson_title']}' "
                                                     "skipped - another lesson is running."})
                else:
                    sch["status"] = "started"
                    await manager.broadcast({"type": "toast", "level": "success",
                                             "text": f"Scheduled lesson '{sch['lesson_title']}' "
                                                     "is starting now!"})
                    await start_lesson_internal(sch["lesson_id"], sch["auto_advance"],
                                                sch["minigame_after_min"])
                await manager.broadcast({"type": "schedule_update",
                                         "schedules": list(schedules.values())})

            if engine.active and engine.status != "paused":
                # ---- auto-advance pacing (robot-led lesson timing) ----
                key = (engine.status, engine.flow_i, len(engine.minigame_queue))
                if key != _aa_key:
                    _aa_key, _aa_since = key, now
                if (auto_advance and engine.status in AA_DURATIONS
                        and now - _aa_since >= AA_DURATIONS[engine.status]
                        and not (engine.status == "review" and engine.minigame_queue)):
                    events = engine.next_step()
                    if engine.current_q:
                        engine.asked_qids.add(engine.current_q["id"])
                    await emit(events)
                    await manager.broadcast({"type": "state", **engine.public_state()})

                # ---- scheduled mini-game moment (mid/late lesson, per teacher timing) ----
                if (_minigame_at_ts and now >= _minigame_at_ts
                        and engine.status in ("content", "review") and not engine.minigame_queue):
                    _minigame_at_ts = None
                    await emit(engine.start_minigame())
                    if engine.minigame_queue:
                        await emit(engine._open_minigame_next())
                    await manager.broadcast({"type": "state", **engine.public_state()})
                perception.tick()
                snap = perception.snapshot()
                if snap.get("audio_event") and now - snap["audio_event"]["ts"] < 1.5:
                    await manager.broadcast({"type": "audio_event", "event": snap["audio_event"]})

                events = engine.tick()                       # auto-close timers
                if events:
                    await emit(events)
                    if any(e.get("type") == "question_closed" for e in events) and engine.last_results:
                        asyncio.create_task(send_review_insight(engine.last_results))
                    if engine.minigame_queue:
                        _minigame_next_at = now + 6

                if _minigame_next_at and now >= _minigame_next_at:
                    _minigame_next_at = None
                    if engine.minigame_queue and engine.status == "review":
                        await emit(engine._open_minigame_next())

                if now - last_analytics >= 5.0:              # 5 s cadence per the paper
                    last_analytics = now
                    students = db.get_students(engine.session_id)
                    if students:
                        result = analytics.compute(
                            engine.session_id, students, snap,
                            engine.questions_asked, db.get_responses(engine.session_id))
                        await manager.broadcast({"type": "engagement", **result})
                        for alert in result.get("alerts", []):
                            db.log_event(engine.session_id, "alert",
                                         f"{alert['name']} engagement dropped to {alert['score']}%")
                            await manager.broadcast({"type": "tracking_alert", **alert})
                        await auto_face_update(result["class_score"])

                if engine.status == "question":
                    await manager.broadcast({
                        "type": "question_tick",
                        "remaining_s": max(0, round(engine.q_deadline - now)),
                        "answers": len(engine.q_answers),
                        "students": len(perception.students),
                    })
        except asyncio.CancelledError:
            return
        except Exception as e:                               # keep the loop alive
            print("tick_loop error:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _speak_queue
    db.init_db()
    db.seed_lessons(Path(__file__).parent / "lessons")
    auth.ensure_default_teacher()
    _speak_queue = asyncio.Queue()
    task = asyncio.create_task(tick_loop())
    speaker_task = asyncio.create_task(_speaker_worker())
    print(f"\n  CAT is ready!  Teacher: http://{lan_ip()}:{PORT}/teacher\n")
    yield
    task.cancel()
    speaker_task.cancel()


app = FastAPI(title="Classroom-Aware Interactive Robot Teacher", lifespan=lifespan)

# Allow any device on the local network to call open endpoints without
# cross-origin blocking. Credentials are intentionally NOT allowed for *; the
# teacher dashboard is protected by cookie auth which only works same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Teacher-only surface. Student endpoints (join/answer/ask/state/seats), the
# robot & projector pages, QR and the WebSocket stay open on the local network.
PROTECTED_PAGES = ("/teacher", "/tracking")
PROTECTED_APIS = ("/api/session/", "/api/robot/", "/api/call_on", "/api/push_question",
                  "/api/perception/", "/api/camera/", "/api/roster",
                  "/api/history/", "/api/events", "/api/asklog", "/api/speechlog",
                  "/api/analytics", "/api/tracking/", "/api/lessons", "/api/schedule",
                  "/api/llm/")


@app.middleware("http")
async def teacher_auth_middleware(request: Request, call_next):
    path = request.url.path
    if not auth.check(request.cookies.get(auth.COOKIE_NAME)):
        if path.startswith(PROTECTED_PAGES):
            return RedirectResponse(f"/login?next={path}", status_code=302)
        if path.startswith(PROTECTED_APIS):
            return JSONResponse({"error": "Teacher login required.", "auth_required": True},
                                status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------- pages
@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/teacher")
async def teacher_page():
    return FileResponse(FRONTEND / "teacher.html")


@app.get("/student")
async def student_page():
    return FileResponse(FRONTEND / "student.html")


@app.get("/robot")
async def robot_page():
    return FileResponse(FRONTEND / "robot.html")


@app.get("/projector")
async def projector_page():
    return FileResponse(FRONTEND / "projector.html")


@app.get("/tracking")
async def tracking_page():
    return FileResponse(FRONTEND / "tracking.html")


@app.get("/login")
async def login_page(request: Request):
    if auth.check(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/teacher", status_code=302)
    return FileResponse(FRONTEND / "login.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND / "manifest.json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(FRONTEND / "sw.js",
                        headers={"Service-Worker-Allowed": "/",
                                 "Content-Type": "application/javascript"})


# ---------------------------------------------------------------- auth api
@app.post("/api/auth/login")
async def auth_login(req: Request):
    body = await req.json()
    token = auth.login(body.get("username", ""), body.get("password", ""))
    if not token:
        return JSONResponse({"error": "Wrong username or password."}, status_code=401)
    resp = JSONResponse({"ok": True, "username": (body.get("username") or "").strip().lower()})
    resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, max_age=auth.SESSION_TTL,
                    samesite="lax", path="/")
    db.log_event(None, "auth", f"Teacher '{body.get('username', '')}' logged in")
    return resp


@app.post("/api/auth/logout")
async def auth_logout(req: Request):
    auth.logout(req.cookies.get(auth.COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
async def auth_me(req: Request):
    username = auth.check(req.cookies.get(auth.COOKIE_NAME))
    return {"authenticated": bool(username), "username": username}


@app.post("/api/auth/change-password")
async def auth_change_password(req: Request):
    username = auth.check(req.cookies.get(auth.COOKIE_NAME))
    if not username:
        return JSONResponse({"error": "Login required.", "auth_required": True}, status_code=401)
    body = await req.json()
    if not auth.change_password(username, body.get("old_password", ""),
                                body.get("new_password", "")):
        return JSONResponse({"error": "Wrong current password, or new password shorter "
                                      "than 6 characters."}, status_code=400)
    return {"ok": True}


# ---------------------------------------------------------------- info / qr
@app.get("/api/info")
async def info():
    ip = lan_ip()
    return {
        "lan_ip": ip, "port": PORT,
        "student_url": f"http://{ip}:{PORT}/student",
        "connections": manager.counts(),
        "perception_mode": perception.mode,
    }


@app.get("/api/qr.svg")
async def qr_svg(path: str = "/student"):
    url = f"http://{lan_ip()}:{PORT}{path}"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    return Response(content=img.to_string(), media_type="image/svg+xml")


# ---------------------------------------------------------------- lessons / session
@app.get("/api/lessons")
async def lessons():
    return db.list_lessons()


@app.get("/api/lessons/template")
async def lesson_template():
    return Response(content=json.dumps(LESSON_TEMPLATE, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=lesson_template.json"})


@app.post("/api/lessons/upload")
async def lesson_upload(req: Request):
    """Teacher uploads a lesson JSON from local disk; validated + stored locally."""
    body = await req.json()
    content = body.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
    errors, lesson = validate_lesson(content)
    if errors:
        return JSONResponse({"error": "Lesson rejected: " + " ".join(errors[:4])},
                            status_code=400)
    existed = db.get_lesson(lesson["id"]) is not None
    (LESSONS_DIR / f"{lesson['id']}.json").write_text(
        json.dumps(lesson, indent=2), encoding="utf-8")
    db.upsert_lesson(lesson)
    db.log_event(None, "lesson_upload",
                 f"Lesson '{lesson['title']}' ({lesson['id']}) uploaded from local disk")
    await manager.broadcast({"type": "lessons_updated"})
    await manager.broadcast({"type": "toast", "level": "success",
                             "text": f"Lesson '{lesson['title']}' "
                                     f"{'updated' if existed else 'added'}!"})
    return {"ok": True, "id": lesson["id"], "title": lesson["title"], "updated": existed,
            "num_questions": len(lesson["questions"])}


@app.post("/api/lessons/from_pdf")
async def lesson_from_pdf(req: Request):
    """Build a whole lesson from a textbook PDF: extract -> KB -> local-LLM MCQs.
    Runs in the background; progress arrives as toasts, then 'lessons_updated'."""
    from . import textbook
    body = await req.json()
    if not llm.detect_local_model():
        return JSONResponse({"error": "Local LLM required - start Ollama first."},
                            status_code=400)
    title = (body.get("title") or "Textbook lesson").strip()[:60]
    lesson_id = re.sub(r"[^a-z0-9_]+", "_", title.lower()).strip("_") or "textbook_lesson"
    num_q = max(2, min(12, int(body.get("num_questions", 8))))
    max_pages = body.get("max_pages")

    if body.get("content_b64"):
        import base64
        uploads = ROOT / "data" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._ -]", "", body.get("filename", "textbook.pdf"))[:80]
        pdf_path = uploads / safe
        pdf_path.write_bytes(base64.b64decode(body["content_b64"]))
    elif body.get("server_path"):
        pdf_path = Path(body["server_path"]).expanduser()
        if not pdf_path.exists():
            return JSONResponse({"error": f"File not found: {pdf_path}"}, status_code=404)
    else:
        return JSONResponse({"error": "Provide content_b64 or server_path."}, status_code=400)

    loop = asyncio.get_running_loop()

    def progress(msg):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "toast", "level": "info", "text": f"\U0001F4DA {msg}"}), loop)

    async def job():
        try:
            lesson = await asyncio.to_thread(
                textbook.build_lesson, pdf_path, lesson_id, title,
                body.get("subject", "Mathematics"), body.get("grade", "Grade 3"),
                num_q, max_pages, progress)
            (LESSONS_DIR / f"{lesson_id}.json").write_text(json.dumps(lesson, indent=2),
                                                           encoding="utf-8")
            db.upsert_lesson(lesson)
            db.log_event(None, "lesson_upload",
                         f"Lesson '{title}' built from textbook {pdf_path.name}")
            await manager.broadcast({"type": "lessons_updated"})
            await manager.broadcast({"type": "toast", "level": "success",
                                     "text": f"\U0001F4DA Lesson '{title}' is ready: "
                                             f"{len(lesson['questions'])} textbook questions!"})
        except Exception as e:
            await manager.broadcast({"type": "toast", "level": "error",
                                     "text": f"Textbook build failed: {e}"})

    asyncio.create_task(job())
    return {"ok": True, "started": True, "lesson_id": lesson_id}


@app.post("/api/session/start")
async def session_start(req: Request):
    body = await req.json()
    ok = await start_lesson_internal(body.get("lesson_id", ""),
                                     body.get("auto_advance", False),
                                     body.get("minigame_after_min"))
    if not ok:
        return JSONResponse({"error": "Unknown lesson"}, status_code=404)
    return engine.public_state()


@app.post("/api/session/autoadvance")
async def session_autoadvance(req: Request):
    global auto_advance
    body = await req.json()
    auto_advance = bool(body.get("enabled", False))
    await manager.broadcast({"type": "autoadvance_state", "enabled": auto_advance})
    await manager.broadcast({"type": "toast", "level": "info",
                             "text": f"Auto-advance {'ON - robot paces the lesson' if auto_advance else 'OFF'}"})
    return {"auto_advance": auto_advance}


# ---------------------------------------------------------------- scheduling
@app.post("/api/schedule")
async def schedule_create(req: Request):
    global _schedule_seq
    body = await req.json()
    lesson = db.get_lesson(body.get("lesson_id", ""))
    if not lesson:
        return JSONResponse({"error": "Unknown lesson"}, status_code=404)
    now = time.time()
    if body.get("due_in_s") is not None:                      # relative (quick-start/tests)
        due_ts = now + float(body["due_in_s"])
    else:
        t = str(body.get("time", ""))
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m:
            return JSONResponse({"error": "Provide time as HH:MM"}, status_code=400)
        date_s = str(body.get("date", "")).strip()
        if date_s:                                            # explicit calendar date
            dm = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date_s)
            if not dm:
                return JSONResponse({"error": "Provide date as YYYY-MM-DD"}, status_code=400)
            d = datetime(int(dm[1]), int(dm[2]), int(dm[3]),
                         int(m[1]), int(m[2]))
            if d.timestamp() < now - 60:
                return JSONResponse({"error": "That date/time has already passed."},
                                    status_code=400)
        else:
            d = datetime.now().replace(hour=int(m[1]), minute=int(m[2]),
                                       second=0, microsecond=0)
            if d.timestamp() < now - 60:                      # already passed -> tomorrow
                d += timedelta(days=1)
        due_ts = d.timestamp()
    mg_after = body.get("minigame_after_min")
    _schedule_seq += 1
    sch = {
        "id": _schedule_seq, "lesson_id": lesson["id"], "lesson_title": lesson["title"],
        "due_ts": due_ts, "auto_advance": bool(body.get("auto_advance", True)),
        "minigame_after_min": float(mg_after) if mg_after else None,
        "status": "pending",
    }
    schedules[sch["id"]] = sch
    db.log_event(None, "schedule", f"'{lesson['title']}' scheduled for "
                                   f"{datetime.fromtimestamp(due_ts).strftime('%H:%M')}")
    await manager.broadcast({"type": "schedule_update", "schedules": list(schedules.values())})
    return sch


@app.post("/api/schedule/{sch_id}/cancel")
async def schedule_cancel(sch_id: int):
    sch = schedules.get(sch_id)
    if not sch:
        return JSONResponse({"error": "Unknown schedule"}, status_code=404)
    if sch["status"] == "pending":
        sch["status"] = "cancelled"
    await manager.broadcast({"type": "schedule_update", "schedules": list(schedules.values())})
    return {"ok": True}


@app.post("/api/session/next")
async def session_next():
    events = engine.next_step()
    if engine.current_q:
        engine.asked_qids.add(engine.current_q["id"])
    await emit(events)
    await manager.broadcast({"type": "state", **engine.public_state()})
    return engine.public_state()


@app.post("/api/session/pause")
async def session_pause():
    await emit(engine.pause())
    await manager.broadcast({"type": "state", **engine.public_state()})
    return engine.public_state()


@app.post("/api/session/resume")
async def session_resume():
    await emit(engine.resume())
    await manager.broadcast({"type": "state", **engine.public_state()})
    return engine.public_state()


@app.post("/api/session/stop")
async def session_stop():
    await emit(engine.end_lesson())
    await manager.broadcast({"type": "state", **engine.public_state()})
    return engine.public_state()


@app.post("/api/session/minigame")
async def session_minigame():
    await emit(engine.start_minigame())
    if engine.minigame_queue:
        await emit(engine._open_minigame_next())
    await manager.broadcast({"type": "state", **engine.public_state()})
    return engine.public_state()


@app.get("/api/state")
async def state():
    st = engine.public_state()
    st["students"] = db.get_students(engine.session_id) if engine.session_id else []
    st["robot_expression"] = robot_expression
    st["auto_face"] = auto_face
    st["server_speaker"] = server_speaker
    st["auto_advance"] = auto_advance
    st["schedules"] = list(schedules.values())
    st["perception_mode"] = perception.mode
    return st


# ---------------------------------------------------------------- students
@app.post("/api/join")
async def join(req: Request):
    """Join with a unique student ID (returning) or as a new anonymous student."""
    if not engine.active:
        return JSONResponse({"error": "No active lesson. Ask your teacher to start one!"},
                            status_code=400)
    body = await req.json()
    code = (body.get("student_code") or "").strip().upper()
    if code:
        roster = db.get_roster_by_code(code)
        if not roster:
            return JSONResponse({"error": f"Unknown student ID '{code}'. "
                                          "Check it or join as a new student."}, status_code=404)
    else:
        roster = db.create_roster_entry()          # no names stored - 'Student N'
    name = roster["name"]

    # Same person re-joining this session (refresh / second device) -> same seat & scores.
    existing = next((s for s in db.get_students(engine.session_id)
                     if s.get("roster_id") == roster["id"]), None)
    if existing:
        return {"student_id": existing["id"], "name": existing["name"], "code": roster["code"],
                "row": existing["seat_row"], "col": existing["seat_col"],
                "lesson": engine.lesson["title"], "rejoined": True}

    row, col = body.get("row"), body.get("col")
    taken = {(s["seat_row"], s["seat_col"]) for s in db.get_students(engine.session_id)}
    if row is None or col is None or (row, col) in taken:
        row, col = next(((r, c) for r in range(4) for c in range(5) if (r, c) not in taken), (0, 0))
    sid = db.add_student(engine.session_id, name, row, col, roster_id=roster["id"])
    perception.add_student(sid, name, row, col, code=roster["code"])
    db.log_event(engine.session_id, "join", f"{name} [{roster['code']}] joined (seat r{row}c{col})")
    await manager.broadcast({"type": "roster", "students": db.get_students(engine.session_id)})
    await manager.broadcast({"type": "toast", "level": "success",
                             "text": f"{name} ({roster['code']}) joined the class!"})
    return {"student_id": sid, "name": name, "code": roster["code"], "row": row, "col": col,
            "lesson": engine.lesson["title"], "rejoined": False}


@app.post("/api/roster")
async def register_student():
    """Teacher pre-registers an anonymous student and hands out the generated ID."""
    entry = db.create_roster_entry()
    await manager.broadcast({"type": "toast", "level": "success",
                             "text": f"Registered {entry['name']} with ID {entry['code']}"})
    return entry


@app.get("/api/seats")
async def seats():
    taken = ({(s["seat_row"], s["seat_col"]): s["name"] for s in db.get_students(engine.session_id)}
             if engine.session_id else {})
    return {"rows": 4, "cols": 5,
            "taken": [{"row": r, "col": c, "name": n} for (r, c), n in taken.items()]}


@app.post("/api/answer")
async def answer(req: Request):
    body = await req.json()
    sid = int(body.get("student_id", 0))
    students = {s["id"]: s for s in db.get_students(engine.session_id)} if engine.session_id else {}
    if sid not in students:
        return JSONResponse({"error": "Unknown student - please rejoin."}, status_code=400)
    question = engine.current_q                           # capture before any auto-close
    result = engine.submit_answer(sid, students[sid]["name"], body.get("question_id"),
                                  int(body.get("answer_index", -1)))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    perception.boost(sid, 12)
    if question:                                          # LLM feedback, non-blocking
        asyncio.create_task(send_answer_feedback(
            sid, students[sid]["name"], question,
            int(body.get("answer_index", -1)), result["correct"]))
    await manager.broadcast({"type": "answer_received",
                             "count": len(engine.q_answers), "total": len(students),
                             "name": students[sid]["name"]})
    if len(engine.q_answers) >= len(students):            # everyone answered -> close early
        global _minigame_next_at
        await emit(engine.close_question())
        if engine.last_results:
            asyncio.create_task(send_review_insight(engine.last_results))
        if engine.minigame_queue:
            _minigame_next_at = time.time() + 6
        await manager.broadcast({"type": "state", **engine.public_state()})
    return result


@app.post("/api/ask")
async def ask(req: Request):
    """Constrained LLM: on-topic questions answered, off-topic politely refused."""
    body = await req.json()
    question = (body.get("question") or "").strip()[:200]
    sid = body.get("student_id")
    if not question:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    result = await asyncio.to_thread(llm.answer, question)   # LLM runs off the event loop
    db.log_ask(engine.session_id, sid, question, result["answer"], result["on_topic"])
    if sid:
        perception.boost(int(sid), 8)
        perception.record_speaking(int(sid))
    students = {s["id"]: s["name"] for s in db.get_students(engine.session_id)} if engine.session_id else {}
    await manager.broadcast({
        "type": "ask", "student": students.get(sid, "Someone"), "question": question,
        "answer": result["answer"], "on_topic": result["on_topic"],
    })
    await emit([
        {"type": "expression", "expression": "thinking" if result["on_topic"] else "apologetic"},
        {"type": "speak", "text": result["answer"]},
    ])
    return result


# ---------------------------------------------------------------- analytics
@app.get("/api/analytics")
async def get_analytics():
    return {
        "students": list(analytics.last_scores.values()),
        "timeline": analytics.timeline[-120:],
        "perception": perception.snapshot(),
    }


@app.get("/api/tracking/live")
async def tracking_live():
    """Snapshot for the live tracking view (page load / refresh)."""
    return {
        "active": engine.active,
        "status": engine.status,
        "lesson": {"id": engine.lesson["id"], "title": engine.lesson["title"]} if engine.lesson else None,
        "students": list(analytics.last_scores.values()),
        "timeline": analytics.timeline[-120:],
        "class_score": analytics.timeline[-1]["class_score"] if analytics.timeline else 0,
        "perception": perception.snapshot(),
    }


@app.get("/api/history/sessions")
async def history_sessions():
    return db.history_sessions()


@app.get("/api/history/sessions/{session_id}")
async def history_session_detail(session_id: int):
    detail = db.history_session_detail(session_id)
    if not detail:
        return JSONResponse({"error": "Unknown session"}, status_code=404)
    return detail


@app.get("/api/history/students")
async def history_students():
    return db.history_students()


@app.get("/api/history/students/{roster_id}")
async def history_student_detail(roster_id: int):
    detail = db.history_student_detail(roster_id)
    if not detail:
        return JSONResponse({"error": "Unknown student"}, status_code=404)
    return detail


@app.get("/api/events")
async def events():
    return db.get_events(engine.session_id) if engine.session_id else []


@app.get("/api/asklog")
async def asklog():
    return db.get_ask_log(engine.session_id) if engine.session_id else []


@app.post("/api/speech")
async def speech(req: Request):
    """Speech-to-text intake: record what a student (or the robot's classroom mic)
    heard, classify whether it is interaction with the teacher/lesson, and count
    it toward the S (speaking) engagement component."""
    if not engine.active:
        return JSONResponse({"error": "No active lesson."}, status_code=400)
    body = await req.json()
    transcript = (body.get("transcript") or "").strip()[:300]
    if not transcript:
        return JSONResponse({"error": "Empty transcript."}, status_code=400)
    sid = body.get("student_id")
    students = {s["id"]: s for s in db.get_students(engine.session_id)}
    if sid is not None:
        sid = int(sid)
        if sid not in students:
            return JSONResponse({"error": "Unknown student - please rejoin."}, status_code=400)
        name = students[sid]["name"]
    else:
        name = "Classroom (robot mic)"

    options = engine.current_q["options"] if engine.status == "question" and engine.current_q else None
    cls = llm.classify_interaction(transcript, options)
    perception.note_real_speech()
    if sid is not None and cls["interaction"]:
        perception.record_speaking(sid)               # feeds the S score (real signal)
        perception.boost(sid, 6)
    db.add_speech(engine.session_id, sid, name, transcript, cls["interaction"], cls["kind"])
    await manager.broadcast({
        "type": "speech", "student_id": sid, "name": name, "transcript": transcript,
        "interaction": cls["interaction"], "kind": cls["kind"],
        "confidence": cls["confidence"], "option": cls.get("option"), "ts": time.time(),
    })
    return cls


@app.get("/api/speechlog")
async def speechlog():
    return db.get_speech_log(engine.session_id) if engine.session_id else []


@app.get("/api/llm/status")
async def llm_status():
    """Which brain is answering: local offline LLM (Ollama) or retrieval-only."""
    return llm.status()


_pending_generated = None
_gen_seq = 0


@app.post("/api/llm/generate_question")
async def llm_generate_question():
    """Local LLM writes a NEW MCQ from the lesson knowledge base (teacher previews it)."""
    global _pending_generated
    if not engine.active:
        return JSONResponse({"error": "Start a lesson first."}, status_code=400)
    if not llm.detect_local_model():
        return JSONResponse({"error": "No local LLM available - install Ollama and pull "
                                      "a model (see README)."}, status_code=400)
    q = await asyncio.to_thread(llm.generate_mcq)
    if not q:
        return JSONResponse({"error": "The model couldn't produce a valid question - "
                                      "try again."}, status_code=502)
    _pending_generated = q
    db.log_event(engine.session_id, "llm_generate", f"AI question drafted: {q['text'][:70]}")
    return q


@app.post("/api/llm/push_generated")
async def llm_push_generated():
    """Teacher approved the AI question -> add it to this session and open it."""
    global _pending_generated, _gen_seq
    if not engine.active:
        return JSONResponse({"error": "No active lesson."}, status_code=400)
    if not _pending_generated:
        return JSONResponse({"error": "Nothing to push - generate a question first."},
                            status_code=400)
    _gen_seq += 1
    q = {"id": f"ai{_gen_seq}", "time_limit": 30, "difficulty": "ai",
         "keywords": [], **_pending_generated}
    engine.lesson["questions"].append(q)
    engine.asked_qids.add(q["id"])
    _pending_generated = None
    await emit(engine.open_question(q["id"]))
    await manager.broadcast({"type": "state", **engine.public_state()})
    db.log_event(engine.session_id, "llm_generate", f"AI question pushed: {q['text'][:70]}")
    return {"ok": True, "id": q["id"]}


@app.post("/api/llm/discard_generated")
async def llm_discard_generated():
    global _pending_generated
    _pending_generated = None
    return {"ok": True}


# ---------------------------------------------------------------- robot controls
@app.post("/api/robot/expression")
async def set_expression(req: Request):
    global _expr_hold_until
    body = await req.json()
    _expr_hold_until = time.time() + 30      # teacher override pauses auto-face for 30 s
    await emit([{"type": "expression", "expression": body.get("expression", "neutral")}])
    return {"ok": True, "auto_face_held_s": 30}


@app.get("/api/tts")
async def tts_endpoint(text: str = ""):
    """Neural robot voice as a WAV - lets any device (robot face page, Docker
    deployments without host audio) play the same happy voice."""
    from starlette.background import BackgroundTask
    from . import tts as tts_mod
    text = (text or "").strip()[:400]
    if not text:
        return JSONResponse({"error": "No text."}, status_code=400)
    wav = await asyncio.to_thread(tts_mod.synth_to_wav, text)
    if not wav:
        return JSONResponse({"error": "Neural voice unavailable."}, status_code=503)
    return FileResponse(wav, media_type="audio/wav",
                        background=BackgroundTask(lambda: Path(wav).unlink(missing_ok=True)))


@app.post("/api/robot/speaker")
async def set_speaker(req: Request):
    """Robot voice through this computer's speaker (macOS say). Toggleable live."""
    global server_speaker
    body = await req.json()
    server_speaker = bool(body.get("enabled", True))
    await manager.broadcast({"type": "speaker_state", "enabled": server_speaker})
    await manager.broadcast({"type": "toast", "level": "info",
                             "text": f"Robot speaker (this computer): {'ON' if server_speaker else 'OFF - browser voice takes over'}"})
    return {"server_speaker": server_speaker}


@app.post("/api/robot/autoface")
async def set_autoface(req: Request):
    global auto_face
    body = await req.json()
    auto_face = bool(body.get("enabled", True))
    await manager.broadcast({"type": "toast", "level": "info",
                             "text": f"Automatic face control {'ON' if auto_face else 'OFF'}"})
    await manager.broadcast({"type": "autoface_state", "enabled": auto_face})
    return {"auto_face": auto_face}


@app.post("/api/robot/say")
async def robot_say(req: Request):
    body = await req.json()
    text = (body.get("text") or "").strip()[:300]
    if text:
        await emit([{"type": "speak", "text": text}])
    return {"ok": True}


@app.post("/api/call_on")
async def call_on(req: Request):
    body = await req.json()
    sid = int(body.get("student_id", 0))
    students = {s["id"]: s for s in db.get_students(engine.session_id)} if engine.session_id else {}
    if sid not in students:
        return JSONResponse({"error": "Unknown student"}, status_code=400)
    await call_on_student({"id": sid, "name": students[sid]["name"]})
    return {"ok": True}


@app.post("/api/push_question")
async def push_question(req: Request):
    body = await req.json()
    sid = int(body.get("student_id", 0))
    students = {s["id"]: s for s in db.get_students(engine.session_id)} if engine.session_id else {}
    if sid not in students:
        return JSONResponse({"error": "Unknown student"}, status_code=400)
    await push_easy_question({"id": sid, "name": students[sid]["name"]})
    await manager.broadcast({"type": "state", **engine.public_state()})
    return {"ok": True}


@app.post("/api/perception/mode")
async def perception_mode(req: Request):
    body = await req.json()
    result = perception.set_mode(body.get("mode", "simulation"))
    await manager.broadcast({"type": "toast", "level": "info",
                             "text": f"Perception mode: {result}"})
    await manager.broadcast({"type": "perception_mode", "mode": perception.mode})
    return {"mode": result}


@app.get("/api/camera/stream")
async def camera_stream():
    """Live annotated webcam feed (MJPEG): face boxes + seating-zone grid."""
    if perception.mode != "camera":
        return JSONResponse({"error": "Camera mode is off - enable it first."}, status_code=400)

    async def gen():
        # wait briefly for the first frame while the camera warms up
        for _ in range(50):
            if perception.camera.latest_jpeg() or not perception.camera.running:
                break
            await asyncio.sleep(0.1)
        while perception.camera.running:
            jpg = perception.camera.latest_jpeg()
            if jpg:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(0.09)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# ---------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    role = ws.query_params.get("role", "viewer")
    await manager.connect(ws, role)
    try:
        await ws.send_text(json.dumps({"type": "state", **engine.public_state()}))
        await ws.send_text(json.dumps({"type": "expression", "expression": robot_expression}))
        await ws.send_text(json.dumps({"type": "speaker_state", "enabled": server_speaker}))
        while True:
            await ws.receive_text()      # keepalive / ignore client messages
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
