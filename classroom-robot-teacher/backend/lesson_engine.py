"""Rule-based lesson engine.

Manages lesson state (greeting, content slides, timed questions, review,
mini-game, ended), scores answers with speed bonuses, and emits UI events
(robot expressions, speech, projector updates). The teacher can pause,
resume, skip, override, or stop at any time - keeping the human in control.
"""
import time

from . import database as db


class LessonEngine:
    def __init__(self):
        self.session_id = None
        self.lesson = None
        self.status = "idle"
        self._paused_from = None
        self.flow_i = -1
        self.current_q = None          # active question dict
        self.q_opened_at = 0.0
        self.q_deadline = 0.0
        self.q_answers = {}            # student_id -> response info (current question)
        self.q_target = None           # student "called on" for this question
        self.q_is_minigame = False
        self.questions_asked = 0
        self.minigame_queue = []
        self.last_results = None

    # ---------------- helpers ----------------
    def _q_by_id(self, qid):
        for q in self.lesson.get("questions", []):
            if q["id"] == qid:
                return q
        return None

    @property
    def active(self) -> bool:
        return self.session_id is not None and self.status not in ("idle", "ended")

    def public_state(self) -> dict:
        remaining = max(0, round(self.q_deadline - time.time())) if self.current_q else 0
        q_pub = None
        if self.current_q:
            q_pub = {k: self.current_q[k] for k in ("id", "text", "options", "time_limit", "difficulty")}
            q_pub["is_minigame"] = self.q_is_minigame
            q_pub["target"] = self.q_target
        flow = self.lesson.get("flow", []) if self.lesson else []
        return {
            "session_id": self.session_id,
            "status": self.status,
            "lesson": {"id": self.lesson["id"], "title": self.lesson["title"],
                       "subject": self.lesson["subject"]} if self.lesson else None,
            "flow_index": self.flow_i,
            "flow_total": len(flow),
            "question": q_pub,
            "question_remaining_s": remaining,
            "questions_asked": self.questions_asked,
            "answers_received": len(self.q_answers),
            "last_results": self.last_results,
        }

    # ---------------- lifecycle ----------------
    def start(self, lesson: dict) -> list:
        self.__init__()                      # reset everything
        self.lesson = lesson
        self.session_id = db.create_session(lesson["id"])
        self.status = "greeting"
        db.log_event(self.session_id, "session", f"Lesson '{lesson['title']}' started")
        greeting = lesson.get("greeting", "Hello class! Let's start our lesson!")
        return [
            {"type": "expression", "expression": "greeting"},
            {"type": "speak", "text": greeting},
            {"type": "stage", "stage": "greeting", "title": lesson["title"],
             "body": greeting, "subject": lesson["subject"]},
        ]

    def next_step(self) -> list:
        """Advance the lesson flow (teacher's Next button, or after review)."""
        if not self.active or self.status == "paused":
            return []
        if self.status == "question":        # skip forward = close current question
            return self.close_question(skipped=True)
        if self.minigame_queue:
            return self._open_minigame_next()

        flow = self.lesson.get("flow", [])
        self.flow_i += 1
        if self.flow_i >= len(flow):
            return self.end_lesson()

        step = flow[self.flow_i]
        if step["type"] == "content":
            self.status = "content"
            db.log_event(self.session_id, "content", step["title"])
            return [
                {"type": "expression", "expression": step.get("expression", "talking")},
                {"type": "speak", "text": step.get("speak", step["body"])},
                {"type": "stage", "stage": "content", "title": step["title"], "body": step["body"]},
            ]
        if step["type"] == "question":
            return self.open_question(step["question_id"])
        return []

    def open_question(self, qid: str, target_student=None, minigame=False) -> list:
        q = self._q_by_id(qid)
        if not q:
            return [{"type": "toast", "level": "error", "text": f"Question {qid} not found"}]
        self.status = "question"
        self.current_q = q
        self.q_opened_at = time.time()
        self.q_deadline = self.q_opened_at + q.get("time_limit", 30)
        self.q_answers = {}
        self.q_target = target_student
        self.q_is_minigame = minigame
        self.questions_asked += 1
        db.log_event(self.session_id, "question", f"Opened {qid}: {q['text'][:60]}")

        speak = q["text"]
        if target_student:
            speak = f"{target_student['name']}, this one is especially for you! {speak}"
        events = [
            {"type": "expression", "expression": "excited" if minigame else "curious"},
            {"type": "speak", "text": speak},
            {"type": "question_open", "question": {
                "id": q["id"], "text": q["text"], "options": q["options"],
                "time_limit": q.get("time_limit", 30), "difficulty": q.get("difficulty", "easy"),
                "is_minigame": minigame,
                "target": {"id": target_student["id"], "name": target_student["name"]} if target_student else None,
            }},
        ]
        if target_student:
            events.append({"type": "toast", "level": "info",
                           "text": f"Robot called on {target_student['name']}"})
        return events

    def submit_answer(self, student_id: int, student_name: str, qid: str, answer_index: int):
        if self.status != "question" or not self.current_q or self.current_q["id"] != qid:
            return {"ok": False, "error": "No open question right now."}
        if student_id in self.q_answers:
            return {"ok": False, "error": "You already answered this question!"}
        elapsed_ms = int((time.time() - self.q_opened_at) * 1000)
        limit_ms = self.current_q.get("time_limit", 30) * 1000
        correct = int(answer_index) == int(self.current_q["answer"])
        points = 0
        if correct:
            points = 100 + int(50 * max(0, (limit_ms - elapsed_ms)) / limit_ms)
            if self.q_is_minigame:
                points *= 2
        info = {"student_id": student_id, "name": student_name, "answer_index": int(answer_index),
                "correct": correct, "points": points, "ms": elapsed_ms}
        self.q_answers[student_id] = info
        db.add_response(self.session_id, student_id, qid, int(answer_index), correct, elapsed_ms, points)
        return {"ok": True, "correct": correct, "points": points,
                "explanation": self.current_q.get("explanation", "")}

    def close_question(self, skipped=False) -> list:
        if not self.current_q:
            return []
        q = self.current_q
        n = len(self.q_answers)
        n_correct = sum(1 for a in self.q_answers.values() if a["correct"])
        dist = [0] * len(q["options"])
        for a in self.q_answers.values():
            dist[a["answer_index"]] += 1
        self.last_results = {
            "question_id": q["id"], "text": q["text"], "options": q["options"],
            "correct_index": q["answer"], "explanation": q.get("explanation", ""),
            "distribution": dist, "answers": n, "correct": n_correct,
            "is_minigame": self.q_is_minigame,
        }
        self.current_q = None
        self.status = "review"
        db.log_event(self.session_id, "review",
                     f"Closed {q['id']}: {n_correct}/{n} correct" + (" (skipped)" if skipped else ""))

        ratio = (n_correct / n) if n else 0
        if n == 0:
            expr, cheer = "concerned", "Hmm, nobody answered that one. Let's talk about it together!"
        elif ratio >= 0.7:
            expr, cheer = "happy", f"Amazing! {n_correct} out of {n} got it right! High five, class!"
        elif ratio >= 0.4:
            expr, cheer = "encouraging", f"Good try! {n_correct} out of {n} were correct. Let's look at the answer!"
        else:
            expr, cheer = "thinking", "That was a tricky one! Let me explain the answer."
        return [
            {"type": "expression", "expression": expr},
            {"type": "speak", "text": cheer + " " + q.get("explanation", "")},
            {"type": "question_closed", "results": self.last_results},
        ]

    # ---------------- mini-game ----------------
    def start_minigame(self) -> list:
        mg = self.lesson.get("minigame")
        if not mg:
            return [{"type": "toast", "level": "error", "text": "This lesson has no mini-game."}]
        self.minigame_queue = list(mg.get("question_ids", []))
        db.log_event(self.session_id, "minigame", f"Mini-game '{mg['title']}' started")
        events = [
            {"type": "expression", "expression": "excited"},
            {"type": "speak", "text": mg.get("intro", "Bonus round! Double points, be quick!")},
            {"type": "minigame_start", "title": mg["title"], "count": len(self.minigame_queue)},
            {"type": "stage", "stage": "minigame", "title": mg["title"],
             "body": mg.get("intro", "Get ready!")},
        ]
        return events

    def _open_minigame_next(self) -> list:
        qid = self.minigame_queue.pop(0)
        return self.open_question(qid, minigame=True)

    # ---------------- teacher controls ----------------
    def pause(self) -> list:
        if not self.active or self.status == "paused":
            return []
        self._paused_from = self.status
        if self.status == "question":       # freeze timer by extending deadline on resume
            self._pause_remaining = max(0, self.q_deadline - time.time())
        self.status = "paused"
        db.log_event(self.session_id, "control", "Lesson paused by teacher")
        return [{"type": "expression", "expression": "sleepy"},
                {"type": "stage", "stage": "paused", "title": "Lesson paused",
                 "body": "The teacher paused the lesson. Eyes up front!"},
                {"type": "speak", "text": "Let's take a short pause. Eyes on your teacher!"}]

    def resume(self) -> list:
        if self.status != "paused":
            return []
        self.status = self._paused_from or "content"
        if self.status == "question" and self.current_q:
            self.q_deadline = time.time() + getattr(self, "_pause_remaining", 10)
            q = self.current_q
            ev = [{"type": "question_open", "question": {
                "id": q["id"], "text": q["text"], "options": q["options"],
                "time_limit": int(getattr(self, "_pause_remaining", 10)),
                "difficulty": q.get("difficulty", "easy"),
                "is_minigame": self.q_is_minigame, "target": None}}]
        else:
            ev = [{"type": "stage", "stage": "content", "title": "We're back!",
                   "body": "Lesson resumed."}]
        db.log_event(self.session_id, "control", "Lesson resumed")
        return [{"type": "expression", "expression": "happy"},
                {"type": "speak", "text": "We're back! Let's keep going!"}] + ev

    def end_lesson(self) -> list:
        closing = self.lesson.get("closing", "That's all for today. Great job, everyone!")
        self.status = "ended"
        db.update_session_status(self.session_id, "ended")
        db.log_event(self.session_id, "session", "Lesson ended")
        return [
            {"type": "expression", "expression": "proud"},
            {"type": "speak", "text": closing},
            {"type": "stage", "stage": "ended", "title": "Lesson complete!", "body": closing},
        ]

    # ---------------- timer tick ----------------
    def tick(self) -> list:
        """Called every second; auto-closes questions and chains mini-game rounds."""
        events = []
        if self.status == "question" and time.time() >= self.q_deadline:
            events += self.close_question()
            if self.minigame_queue:
                events.append({"type": "toast", "level": "info", "text": "Next mini-game question in 6s..."})
        return events
