"""Engagement analytics.

Implements the capstone score model:

    Participation Score = 1/4(A) + 1/4(P) + 1/4(Q) + 1/4(S)

where A = attention level (vision), P = participation frequency (answers
submitted vs. questions asked), Q = quiz performance (correctness), and
S = speaking interaction (voice activity attributed by localization).

Scores are recomputed every 5 seconds (per the paper) and color-coded for
the live views; history is stored in SQLite for the tracking page.
"""
import time

from . import database as db


def color_for(score: float) -> str:
    if score >= 70:
        return "green"
    if score >= 45:
        return "yellow"
    if score > 0:
        return "red"
    return "gray"


class Analytics:
    def __init__(self):
        self.timeline = []          # [{ts, class_score, class_attention}]
        self.last_scores = {}       # student_id -> score dict
        self.student_history = {}   # student_id -> [{ts, score, attention}] (rolling)

    def reset(self):
        self.timeline = []
        self.last_scores = {}
        self.student_history = {}

    def compute(self, session_id: int, students: list, perception_snap: dict,
                questions_asked: int, responses: list) -> dict:
        """Recompute per-student A/P/Q/S + composite score. Persists samples."""
        by_student = {}
        for r in responses:
            by_student.setdefault(r["student_id"], []).append(r)

        speaking_counts = perception_snap.get("speaking_counts", {})
        max_speak = max(speaking_counts.values()) if speaking_counts else 0
        now = time.time()
        scores = {}
        alerts = []
        for s in students:
            sid = s["id"]
            resp = by_student.get(sid, [])
            a = perception_snap["attention"].get(sid, 50.0)
            p = (len(resp) / questions_asked * 100.0) if questions_asked else 0.0
            p = min(p, 100.0)
            answered = len(resp)
            q = (sum(r["is_correct"] for r in resp) / answered * 100.0) if answered else 0.0
            speaks = speaking_counts.get(sid, 0)
            sc = (speaks / max_speak * 100.0) if max_speak else 0.0
            score = 0.25 * a + 0.25 * p + 0.25 * q + 0.25 * sc

            hist = self.student_history.setdefault(sid, [])
            hist.append({"ts": now, "score": round(score, 1), "attention": round(a, 1)})
            if len(hist) > 120:                           # keep ~10 min at 5 s cadence
                del hist[:-120]
            prev = self.last_scores.get(sid, {}).get("score")
            if prev is not None and prev >= 45.0 > score:
                alerts.append({"student_id": sid, "name": s["name"],
                               "score": round(score, 1), "ts": now})

            scores[sid] = {
                "student_id": sid, "name": s["name"], "roster_id": s.get("roster_id"),
                "row": s["seat_row"], "col": s["seat_col"],
                "attention": round(a, 1), "participation": round(p, 1),
                "quiz": round(q, 1), "speaking": round(sc, 1),
                "score": round(score, 1), "color": color_for(score),
                "answered": answered,
                "correct": sum(r["is_correct"] for r in resp),
                "points": sum(r.get("points", 0) for r in resp),
                "history": hist[-24:],
            }
            db.add_engagement_sample(session_id, sid, a, p, q, sc, score)

        self.last_scores = scores
        class_score = round(sum(v["score"] for v in scores.values()) / len(scores), 1) if scores else 0.0
        point = {
            "ts": now,
            "class_score": class_score,
            "class_attention": perception_snap.get("class_attention", 0.0),
        }
        self.timeline.append(point)
        if len(self.timeline) > 600:                      # keep ~50 min at 5 s cadence
            self.timeline = self.timeline[-600:]
        return {
            "students": list(scores.values()),
            "class_score": class_score,
            "class_attention": point["class_attention"],
            "timeline": self.timeline[-120:],
            "alerts": alerts,
        }
