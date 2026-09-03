# Classroom-Aware-Robot-Interactive-Teacher-
# CAT - Classroom-Aware Interactive Robot Teacher

A locally hosted, privacy-first classroom teaching assistant that turns an ordinary
computer and webcam into an interactive robot teacher. It runs lesson scripts,
monitors student engagement with computer vision, accepts real-time quiz
responses from phones or tablets, answers on-topic questions with a constrained
local language model, and speaks to the class through the computer's speakers or
browser. No cloud connection is required after the initial model download.

## Overview

Traditional classroom tools often force teachers to choose between rich analytics
and student privacy. Commercial platforms capture video, voice, and quiz data on
remote servers, which can conflict with school policies and parental consent.
CAT reverses that assumption: every video frame, voice utterance, quiz answer,
and LLM inference stays on the teacher's machine and the local network. The
system is designed as a portable, offline-first prototype that can be set up in
minutes, carried to any classroom, and packed away as a Docker image.

The project originated as a capstone at Abu Dhabi University. It explores
whether an affordable, compact, and safe robot teacher can improve engagement by
combining attention sensing, participation tracking, automatic speech, and
controlled AI assistance in a single integrated package. The result is a
multi-screen web application with dedicated views for the teacher, the students,
the robot face, the projector, and live student tracking.

## What the system does

CAT orchestrates a complete lesson from start to finish. A teacher selects or
uploads a lesson, starts it from the dashboard, and the robot guides the class
through greeting slides, content, multiple-choice questions, review moments, and
a quick-fire mini-game. Students join from their own devices by scanning a QR
code, pick a seat, and answer questions as the lesson flows. Meanwhile, the
perception engine watches the classroom through a webcam and updates engagement
scores every five seconds.

Six views share the same backend state over WebSockets:

- **Landing page** - shows the QR code and the student URL for the current LAN.
- **Teacher dashboard** - the command center for lessons, robot control,
  scheduling, and live logs.
- **Student app** - the mobile-first interface students use to join, answer, and
  ask the robot questions.
- **Robot face** - an animated expression screen with text-to-speech output.
- **Projector view** - full-screen questions, timers, leaderboards, and results
  for the big display.
- **Student tracking** - teacher-only live monitoring and cross-session analytics.

All views can be opened on different devices on the same local network; the
teacher view is password protected, while the student, robot, projector, and
public APIs remain open.

## Key features

### Lesson engine and scheduling

Lessons are stored as JSON scripts with a linear sequence of greeting, content,
question, review, and mini-game stages. The engine handles timing, scoring,
leaderboards, and transitions. Teachers can start, pause, resume, step forward,
and end lessons manually. They can also schedule a lesson for a future time or
enable auto-advance, in which the robot paces greeting, content, and review
slides automatically. A timed mini-game can be launched manually or scheduled to
run after a chosen number of minutes.

The repository ships with two example lessons. Teachers can upload their own JSON
files, download a template, or generate a lesson from a textbook PDF. The PDF
pipeline extracts text, removes workbook noise, creates a local knowledge base,
and uses the local LLM to draft grounded multiple-choice questions that the
teacher previews before accepting.
File Layout 
classroom-robot-teacher/
├── backend/                    # Python FastAPI server
│   ├── main.py                 # App entry, routes, WebSocket
│   ├── database.py             # SQLite schema and queries
│   ├── auth.py                 # Teacher login and sessions
│   ├── analytics.py            # Engagement score computation
│   ├── lesson_engine.py        # Lesson state and pacing
│   ├── perception.py           # Camera/gaze and simulated attention
│   ├── llm.py                  # Constrained local LLM client
│   ├── tts.py                  # Text-to-speech (Kokoro + fallback)
│   ├── textbook.py             # PDF extraction and MCQ generation
│   └── lessons/                # Built-in and uploaded lesson JSONs
├── frontend/                   # HTML/CSS/JS UI
│   ├── index.html              # Landing + QR code
│   ├── login.html              # Teacher login
│   ├── teacher.html            # Teacher dashboard
│   ├── tracking.html           # Live + history tracking
│   ├── student.html            # Student app
│   ├── robot.html              # Robot face
│   ├── projector.html          # Projector view
│   ├── manifest.json           # PWA manifest
│   ├── sw.js                   # Service worker
│   └── static/
│       ├── style.css           # Shared design system
│       ├── common.js           # Shared helpers and PWA setup
│       ├── offline.html        # Offline fallback
│       └── icons/              # App icons
├── data/                       # Runtime data
│   ├── cat.db                  # SQLite database
│   └── tts/                    # Kokoro TTS model files
├── README.md                   # Original project README
├── README_GITHUB.md            # Polished GitHub description
├── Dockerfile                  # Container build
├── docker-compose.yml          # Docker stack
├── docker-entrypoint.sh        # Container startup
├── requirements.txt            # Python dependencies
└── run.sh                      # Native macOS/Linux launcher

### Engagement analytics

The capstone score model is implemented directly:

```
Score = 1/4(A) + 1/4(P) + 1/4(Q) + 1/4(S)
```

where A is attention, P is participation, Q is quiz performance, and S is
speaking interaction. The analytics module recomputes this every five seconds and
broadcasts the result to all connected clients. A live class timeline chart on
the tracking page shows the class score over time, and each student card shows
the four component bars and a recent-history sparkline.

### Perception and camera monitoring

The perception system can operate in two modes. Simulation mode provides
generated attention values for each seat column and simulated audio-source
localization. Camera mode uses OpenCV Haar cascades on the teacher's webcam to
detect frontal and profile faces, classify gaze states, and split the classroom
into five horizontal zones. A detected face with two eyes is "focused", one eye
is "partial", eyes hidden is "down", a profile face is "away", and an empty zone
is "empty". These states drive student attention values in real time. Camera
frames are processed in memory and are never stored.

### Constrained local LLM

The assistant is powered by Ollama running a local model such as `llama3.2:1b` or
`llama3.2:3b`. A retrieval layer first checks whether a student question is
grounded in the current lesson. Off-topic questions are politely refused and
never sent to the model. On-topic questions receive an answer based on the
approved lesson facts. The same model is used to write personalized feedback for
each MCQ answer, to summarize the class response distribution when a question
closes, and to generate new multiple-choice questions from a textbook PDF.

Because the model is local, it can run without an internet connection after the
first download. The `GET /api/llm/status` endpoint reports whether the model is
ready.

### Robot face, voice, and automated expressions

The robot face displays animated expressions such as happy, neutral, thinking,
concerned, and excited. An optional automated mode maps the live class
engagement score to an expression: happy above 80, neutral between 60 and 79,
thinking between 45 and 59, and concerned below 45. The robot also speaks to the
class using a local Kokoro neural voice when `afplay` is available, falling back
to the browser text-to-speech on other devices. Speech events are queued so that
phrases never cut each other off.

Pre-written speech buttons let the teacher trigger greetings, attention prompts,
praise, and class-flow phrases. Custom phrases can be added and saved locally.

### Anonymous student identity

No personal names are stored. A joining student is automatically assigned a
numbered, anonymous identity such as `Student 7 / S-007`. Returning students can
re-enter their ID to continue their history. The SQLite database tracks
attendance, engagement, quiz accuracy, points, and cross-session trends without
ever recording a real name. Legacy databases are automatically anonymized on
startup.

### Persistent tracking

The tracking page gives teachers both live and historical insights. The live tab
shows per-student cards with engagement sparklines, attention and quiz bars, and
low-engagement alerts. The students tab lists attendance, average engagement,
quiz accuracy, points, and trend arrows. The sessions tab archives past lessons
with class timelines and per-student results. All history is stored locally in
SQLite.

### Progressive Web App support

The front end is now PWA-capable. A web app manifest, service worker, offline
page, and app icons allow the landing page and student view to be added to a
device home screen. CSS breakpoints reflow the teacher and tracking dashboards
for phones and tablets, and touch targets are sized for mobile use. Students can
join and answer from their own phones over the same Wi-Fi network.

## Technology stack

- **Backend**: Python 3, FastAPI, Uvicorn, SQLite.
- **Frontend**: Vanilla HTML, CSS, and JavaScript; WebSocket real-time updates;
  PWA manifest and service worker.
- **Perception**: OpenCV 4.x with Haar cascades for face and eye detection.
- **LLM**: Ollama with `llama3.2` models for offline inference.
- **TTS**: Kokoro ONNX for natural neural voice; `afplay` fallback.
- **PDF**: pypdf for extracting textbook content.
- **Deployment**: native `./run.sh` or Docker Compose with a Ollama sidecar.

## Architecture

The backend is a single FastAPI process. At startup it initializes the SQLite
database, seeds the bundled lessons, creates a default teacher account, and
spawns background workers for lesson scheduling, speaker output, and the
five-second analytics tick. REST endpoints handle lesson control, student
join/answer, question asking, analytics, robot commands, camera toggling, and
history. WebSockets broadcast live events such as score changes, new questions,
and robot expressions to every connected view.

The database is a single `data/cat.db` file. It stores lessons, sessions,
students, roster, responses, engagement samples, event logs, schedules, and the
Ask-CAT log. This keeps the entire classroom record on the local machine and
makes backups as simple as copying one file.

The frontend is intentionally framework-free. Each view is a plain HTML page that
loads the shared `style.css` and `common.js`. The robot face, projector, student,
and teacher pages talk to the same backend over the same WebSocket channel, so
all screens stay in sync without a heavy client-side build step.

## Running the project

### Native (macOS / Linux)

Install Ollama, pull a model, then:

```bash
cd classroom-robot-teacher
./run.sh
```

The script starts Ollama if it is not running, creates a Python virtual
environment if needed, and launches Uvicorn on port 8000. The teacher dashboard
is at `http://localhost:8000/teacher`; students scan the QR code or open the
displayed LAN URL.

### Docker

On any device with Docker Desktop or Docker Engine:

```bash
CAT_PUBLIC_HOST=<your-LAN-IP> docker compose up -d
```

This starts the CAT container and an Ollama container with persistent volumes
for the database, models, and neural voice. The first launch downloads the
Ollama model and the TTS model, so it may take a few minutes.

## Privacy and security notes

The entire application is designed for local classroom use. No video, audio,
quiz answers, or lesson data is sent to the cloud. The default teacher account is
seeded on first run and should be given a strong password as soon as possible.
Teacher-only pages and APIs are protected by a session cookie; protected API
endpoints return `401` when the cookie is missing.

Camera monitoring is optional and controlled by the teacher from the tracking
page. No frames are stored or transmitted beyond the live browser feed. The
browser speech recognition feature is gated to secure contexts (localhost or
HTTPS) for privacy reasons.

## Future directions

Planned and possible enhancements include dedicated native mobile apps, richer
scheduling with recurrence, voice selection for the robot, multi-camera and
microphone arrays, more advanced gaze models, automated post-class PDF reports,
and richer cross-session predictive analytics. The current prototype provides the
offline core on which these additions can be built.

## License and attribution

CAT is a capstone prototype. It was built for research and classroom
demonstration. Please review the original capstone paper and project constraints
before deploying in a production school environment.
