"""Perception system: attention tracking + sound-source localization.

Two modes (switchable live from the dashboard / tracking page):

  - simulation : per-student attention as a bounded random walk + simulated
    GCC-PHAT style sound-source localization (no hardware needed).
  - camera     : REAL face detection on the local webcam (OpenCV Haar cascade).
    Detected faces are mapped to 5 seating-zone columns (left..right of the
    frame = seat columns 1..5); students seated in a zone where a face is
    visible gain attention, empty zones decay. An annotated MJPEG stream
    (bounding boxes + zone grid) is served to the teacher's tracking page.

All processing is local - frames never leave this machine and are never
stored (privacy by design, per the capstone constraints).
"""
import random
import threading
import time

try:
    import cv2  # optional - only used in camera mode
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False

ZONES = 5
ZONE_NAMES = ["far-left", "left", "center", "right", "far-right"]


GAZE_TARGET = {"focused": 95.0, "partial": 75.0, "down": 55.0, "away": 30.0, "empty": 22.0}
GAZE_RANK = {"focused": 4, "partial": 3, "down": 2, "away": 1, "empty": 0}


class Camera:
    """Webcam capture + face & GAZE detection + annotated JPEG frames.

    Gaze estimation (classic OpenCV cascades, all local):
      - frontal face + 2 eyes visible  -> 'focused'  (looking at robot/board)
      - frontal face + 1 eye           -> 'partial'
      - frontal face + no eyes         -> 'down'     (reading/writing/phone)
      - profile face only              -> 'away'     (head turned sideways)

    On macOS the camera-permission prompt can only be triggered from the MAIN
    thread, so the device is opened in start() (called from the API handler)
    and only the read/detect/encode loop runs in a background thread.
    """

    def __init__(self):
        self.running = False
        self.error = None
        self.faces = 0
        self.zone_faces = [0] * ZONES
        self.zone_gaze = ["empty"] * ZONES
        self.zone_labels = [""] * ZONES   # student IDs seated in each column
        self.gaze_counts = {"focused": 0, "partial": 0, "down": 0, "away": 0}
        self._jpeg = None
        self._lock = threading.Lock()
        self._thread = None
        self._boxes = []          # (x, y, w, h, gaze)
        self._cap = None

    def start(self) -> bool:
        if self.running:
            return True
        if not HAS_CV2:
            self.error = "OpenCV not installed"
            return False
        if self._thread and self._thread.is_alive():   # let a previous run release the device
            self._thread.join(timeout=2)
        self.error = None

        # Open on the calling (main) thread so macOS can show the permission dialog.
        cap = cv2.VideoCapture(0)
        if not cap.isOpened() or not cap.read()[0]:
            cap.release()
            self.error = ("Camera not authorized or unavailable. macOS: System Settings > "
                          "Privacy & Security > Camera - enable it for your terminal/editor "
                          "app, then restart the server and try again.")
            print(f"[camera] {self.error}")
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self._cap = cap
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self.running = False
        self.faces = 0
        self.zone_faces = [0] * ZONES
        self.zone_gaze = ["empty"] * ZONES
        self.gaze_counts = {"focused": 0, "partial": 0, "down": 0, "away": 0}

    def latest_jpeg(self):
        with self._lock:
            return self._jpeg

    def _detect(self, gray, w) -> None:
        """Classify each visible person's gaze and aggregate per seating zone."""
        found = self._frontal.detectMultiScale(gray, scaleFactor=1.15,
                                               minNeighbors=5, minSize=(60, 60))
        boxes = []
        for (x, y, fw, fh) in found:
            eye_roi = gray[y:y + int(fh * 0.55), x:x + fw]       # eyes live in the upper half
            eyes = self._eye.detectMultiScale(eye_roi, scaleFactor=1.1, minNeighbors=4,
                                              minSize=(max(14, fw // 8),) * 2)
            gaze = "focused" if len(eyes) >= 2 else ("partial" if len(eyes) == 1 else "down")
            boxes.append((x, y, fw, fh, gaze))

        # profile faces = head turned away; scan both orientations
        profiles = list(self._profile.detectMultiScale(gray, 1.15, 5, minSize=(60, 60)))
        flipped = cv2.flip(gray, 1)
        for (x, y, fw, fh) in self._profile.detectMultiScale(flipped, 1.15, 5, minSize=(60, 60)):
            profiles.append((w - x - fw, y, fw, fh))
        for (x, y, fw, fh) in profiles:
            cx, cy = x + fw / 2, y + fh / 2
            overlaps = any(abs(cx - (bx + bw / 2)) < bw * 0.6 and abs(cy - (by + bh / 2)) < bh * 0.6
                           for (bx, by, bw, bh, _g) in boxes)
            if not overlaps:
                boxes.append((x, y, fw, fh, "away"))

        zf, zg = [0] * ZONES, ["empty"] * ZONES
        counts = {"focused": 0, "partial": 0, "down": 0, "away": 0}
        for (x, y, fw, fh, gaze) in boxes:
            zone = min(ZONES - 1, int((x + fw / 2) / w * ZONES))
            zf[zone] += 1
            if GAZE_RANK[gaze] > GAZE_RANK[zg[zone]]:
                zg[zone] = gaze
            counts[gaze] += 1
        self._boxes, self.faces = boxes, len(boxes)
        self.zone_faces, self.zone_gaze, self.gaze_counts = zf, zg, counts

    def _loop(self) -> None:
        cap = self._cap
        hc = cv2.data.haarcascades
        self._frontal = cv2.CascadeClassifier(hc + "haarcascade_frontalface_default.xml")
        self._eye = cv2.CascadeClassifier(hc + "haarcascade_eye.xml")
        self._profile = cv2.CascadeClassifier(hc + "haarcascade_profileface.xml")
        colors = {"focused": (153, 211, 52), "partial": (36, 191, 251),
                  "down": (36, 150, 251), "away": (113, 113, 248)}
        status_text = {"focused": "looking", "partial": "partly looking",
                       "down": "looking down", "away": "not looking"}
        frame_i = 0
        fail_streak = 0
        while self.running:
            ok, frame = cap.read()
            if not ok:
                fail_streak += 1
                if fail_streak > 30:
                    self.error = ("Camera stopped delivering frames - it may be in use "
                                  "by another app. Stop and restart camera monitoring.")
                    self.running = False
                    break
                time.sleep(0.2)
                continue
            fail_streak = 0
            h, w = frame.shape[:2]

            if frame_i % 6 == 0:               # detect ~2x/second at ~12 fps
                self._detect(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), w)
            frame_i += 1

            # annotate: zone grid + gaze-colored face boxes + "ID - looking?" labels
            for z in range(1, ZONES):
                x = int(w * z / ZONES)
                cv2.line(frame, (x, 0), (x, h), (60, 70, 90), 1)
            for (x, y, fw, fh, gaze) in self._boxes:
                c = colors.get(gaze, (238, 211, 34))
                cv2.rectangle(frame, (x, y), (x + fw, y + fh), c, 2)
                zone = min(ZONES - 1, int((x + fw / 2) / w * ZONES))
                sid = self.zone_labels[zone]
                label = f"{sid} - {status_text[gaze]}" if sid else status_text[gaze]
                (tw, th), _b = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
                ty = y - 10 if y - 10 > th else y + fh + th + 8
                cv2.rectangle(frame, (x - 2, ty - th - 6), (x + tw + 6, ty + 6), c, -1)
                cv2.putText(frame, label, (x + 2, ty),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (15, 18, 32), 2)
            gc = self.gaze_counts
            cv2.putText(frame, f"faces: {self.faces}  focused: {gc['focused']}  away: {gc['away']}",
                        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (153, 211, 52), 2)
            okj, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if okj:
                with self._lock:
                    self._jpeg = buf.tobytes()
            time.sleep(0.08)                   # ~12 fps
        cap.release()
        self._cap = None
        with self._lock:
            self._jpeg = None


class Perception:
    def __init__(self):
        self.mode = "simulation"          # "simulation" | "camera"
        self.students = {}                # id -> state dict
        self.audio_event = None           # latest sound-source event
        self.camera = Camera()
        self.last_real_speech = 0.0       # real STT activity suppresses simulated audio

    def note_real_speech(self) -> None:
        self.last_real_speech = time.time()

    # ---------------- student lifecycle ----------------
    def add_student(self, student_id: int, name: str, row: int, col: int, code: str = "") -> None:
        self.students[student_id] = {
            "name": name, "row": row, "col": col,
            "code": code or name.replace("Student ", "S-"),
            "attention": random.uniform(65, 90),
            "mood": random.choice(["focused", "focused", "normal", "dreamy"]),
            "speaking_events": 0,
            "last_boost": 0.0,
        }
        self._update_camera_labels()

    def reset_students(self) -> None:
        self.students.clear()
        self._update_camera_labels()

    def _update_camera_labels(self) -> None:
        """Map each seat column to the IDs of the students seated there."""
        labels = [[] for _ in range(ZONES)]
        for s in self.students.values():
            zone = max(0, min(ZONES - 1, s["col"]))
            labels[zone].append(s["code"])
        self.camera.zone_labels = [",".join(z) for z in labels]

    def boost(self, student_id: int, amount: float = 12.0) -> None:
        """Interaction (answering/asking) re-engages a student."""
        s = self.students.get(student_id)
        if s:
            s["attention"] = min(100.0, s["attention"] + amount)
            s["last_boost"] = time.time()

    def record_speaking(self, student_id: int) -> None:
        s = self.students.get(student_id)
        if s:
            s["speaking_events"] += 1

    # ---------------- tick (called ~1/s) ----------------
    def tick(self) -> None:
        now = time.time()
        if self.mode == "camera" and self.camera.running:
            self._camera_attention(now)
        else:
            self._simulated_attention(now)

        # voice activity + sound-source localization (mic array simulated);
        # paused while REAL speech (student mics / robot mic STT) is coming in
        if self.students and random.random() < 0.18 and now - self.last_real_speech > 120:
            sid = random.choice(list(self.students.keys()))
            s = self.students[sid]
            azimuth = (s["col"] - 2) * 30 + random.uniform(-8, 8)   # cols 0..4 -> -60..+60 deg
            self.audio_event = {
                "student_id": sid, "name": s["name"],
                "azimuth_deg": round(azimuth, 1),
                "zone": ZONE_NAMES[max(0, min(ZONES - 1, s["col"]))],
                "ts": now,
            }
            s["speaking_events"] += 1

    def _simulated_attention(self, now: float) -> None:
        for s in self.students.values():
            drift = {"focused": 1.0, "normal": 0.0, "dreamy": -1.2}[s["mood"]]
            s["attention"] += random.uniform(-4, 4) + drift
            if random.random() < 0.03:                     # random distraction dip
                s["attention"] -= random.uniform(10, 25)
            if random.random() < 0.02:                     # mood change
                s["mood"] = random.choice(["focused", "normal", "dreamy"])
            if now - s["last_boost"] < 20:                 # recent interaction keeps focus
                s["attention"] += 0.8
            s["attention"] = max(5.0, min(100.0, s["attention"]))

    def _camera_attention(self, now: float) -> None:
        """Real signal: GAZE state of the student's seat-column zone drives attention."""
        zg = self.camera.zone_gaze
        for s in self.students.values():
            zone = max(0, min(ZONES - 1, s["col"]))
            target = GAZE_TARGET[zg[zone]]
            s["attention"] = 0.75 * s["attention"] + 0.25 * target + random.uniform(-2, 2)
            if now - s["last_boost"] < 20:
                s["attention"] += 0.8
            s["attention"] = max(5.0, min(100.0, s["attention"]))

    # ---------------- mode control ----------------
    def set_mode(self, mode: str) -> str:
        if mode == "camera":
            if not HAS_CV2:
                self.mode = "simulation"
                return "simulation (OpenCV not installed - run: pip install opencv-python)"
            if not self.camera.start():
                self.mode = "simulation"
                return f"simulation ({self.camera.error})"
            self.mode = "camera"
        else:
            self.mode = "simulation"
            self.camera.stop()
        return self.mode

    # ---------------- snapshot ----------------
    def snapshot(self) -> dict:
        att = {sid: round(s["attention"], 1) for sid, s in self.students.items()}
        class_att = round(sum(att.values()) / len(att), 1) if att else 0.0
        return {
            "mode": self.mode,
            "attention": att,
            "class_attention": class_att,
            "audio_event": self.audio_event,
            "camera_faces": self.camera.faces,
            "camera_zones": self.camera.zone_faces,
            "camera_zone_gaze": self.camera.zone_gaze,
            "gaze_counts": self.camera.gaze_counts,
            "camera_running": self.camera.running,
            "camera_error": self.camera.error,
            "speaking_counts": {sid: s["speaking_events"] for sid, s in self.students.items()},
        }
