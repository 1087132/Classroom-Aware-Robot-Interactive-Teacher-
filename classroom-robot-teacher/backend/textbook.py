"""Textbook -> lesson pipeline.

Extracts a textbook PDF locally (pypdf), cleans workbook noise, builds a
teacher-approved knowledge base from the instructional text, and asks the
LOCAL LLM to draft MCQs grounded in the book. Both the ask-CAT answers and
the generated questions then come from the textbook. Everything runs offline.

CLI:  python -m backend.textbook "<pdf>" --title "Grade 3 Maths" --subject Mathematics \
          --grade "Grade 3" --id grade3_math --questions 8
"""
import json
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from .llm import ConstrainedLLM, _tokens

LESSONS_DIR = Path(__file__).resolve().parent / "lessons"

SKIP_SNIPPETS = (
    "preface", "acknowledg", "teacher's remarks", "profiling",
    "good satisfactory needs improvement", "copyright", "isbn",
    "all rights reserved", "ministry of education",
)


EXERCISE_ARTIFACTS = re.compile(
    r"(let'?s work out|try these|it corner|new words|teacher'?s remarks|"
    r"activity:?|\bexample\b:?|i (write|match|draw|circle|colou?r|complete|fill)\b[^.?!]*|"
    r"with the help of your teacher[^.?!]*|\(\s*[a-f]\s*\))", re.I)

# heuristics: things a question CAN'T reference without seeing the workbook page
CONTEXT_BOUND = ("picture", "figure", "diagram", "abacus", "shown", "above", "below",
                 "in the box", "basket", "beads", "strips", "workbook", "page")


def _teachable_score(chunk: str) -> int:
    """How suitable a chunk is for drafting a standalone question."""
    cl = chunk.lower()
    score = 0
    if "remember" in cl:
        score += 2                              # 'Remember, Friends!' definition blocks
    if re.search(r"\b(is|are|has|have|means|called|when you|we use|there are)\b", cl):
        score += 1
    score -= min(2, len(EXERCISE_ARTIFACTS.findall(chunk)))
    if any(w in cl for w in CONTEXT_BOUND):
        score -= 2
    return score


def extract_chunks(pdf_path, max_pages=None) -> list:
    """Clean instructional text chunks (~120-450 chars) from a textbook PDF."""
    reader = PdfReader(str(pdf_path))
    pages = reader.pages[:max_pages] if max_pages else reader.pages
    chunks = []
    for page in pages:
        text = re.sub(r"\s+", " ", page.extract_text() or "").strip()
        if not text or any(s in text.lower()[:300] for s in ("preface", "acknowledg", "contents")):
            continue
        text = EXERCISE_ARTIFACTS.sub(" ", text)          # strip workbook noise
        text = re.sub(r"\s+", " ", text)
        keep = []
        for sent in re.split(r"(?<=[.!?:]) +", text):
            words = sent.split()
            alpha = sum(1 for w in words if re.search(r"[a-zA-Z]{2,}", w))
            if (len(words) >= 5 and alpha / len(words) >= 0.55
                    and not any(sk in sent.lower() for sk in SKIP_SNIPPETS)):
                keep.append(sent.strip())
        buf = ""
        for sent in keep:
            if len(buf) + len(sent) < 450:
                buf = (buf + " " + sent).strip()
            else:
                if len(buf) >= 120:
                    chunks.append(buf)
                buf = sent
        if len(buf) >= 120:
            chunks.append(buf)
    seen, out = set(), []
    for c in chunks:                       # drop near-duplicates
        key = c.lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _keywords(text: str, n=5) -> list:
    return [w for w, _c in Counter(_tokens(text)).most_common(n)]


def build_kb(chunks: list) -> list:
    kb = []
    for c in chunks:
        first = re.split(r"(?<=[.!?]) ", c)[0][:90]
        kb.append({"q": first, "a": c, "keywords": _keywords(c)})
    return kb


def build_lesson(pdf_path, lesson_id, title, subject="Mathematics", grade="Grade 3",
                 num_questions=8, max_pages=None, progress=None) -> dict:
    """Full pipeline: PDF -> chunks -> KB -> LLM-drafted MCQs -> lesson dict."""
    def report(msg):
        if progress:
            progress(msg)

    report("Extracting textbook text...")
    chunks = extract_chunks(pdf_path, max_pages=max_pages)
    if len(chunks) < 3:
        raise ValueError("Could not extract enough readable text from this PDF.")
    kb = build_kb(chunks)
    report(f"Built knowledge base: {len(kb)} passages. Drafting questions with the local LLM...")

    llm = ConstrainedLLM()
    llm.topic = title
    if not llm.detect_local_model():
        raise RuntimeError("No local LLM available (start Ollama / pull a model).")

    import random as _random
    # draft questions only from the most teachable passages (definitions first)
    ranked = sorted(range(len(kb)), key=lambda i: _teachable_score(kb[i]["a"]), reverse=True)
    pool = [i for i in ranked if _teachable_score(kb[i]["a"]) >= 1] or ranked[:20]
    _random.shuffle(pool)
    questions, used_texts = [], set()
    for idx in pool:
        if len(questions) >= num_questions:
            break
        # two-step: distill a self-contained fact first, then draft the MCQ from it
        fact = llm.distill_fact(kb[idx]["a"])
        if not fact:
            continue
        q = llm.generate_mcq(fact=fact)
        if (not q or q["text"].lower() in used_texts
                or any(w in q["text"].lower() for w in CONTEXT_BOUND)):
            continue
        used_texts.add(q["text"].lower())
        q.update({"id": f"q{len(questions) + 1}", "time_limit": 30, "difficulty": "easy",
                  "keywords": kb[idx]["keywords"]})
        questions.append(q)
        report(f"Question {len(questions)}/{num_questions} drafted...")
    if len(questions) < 2:
        raise RuntimeError("The local LLM could not draft enough valid questions from this book.")

    # a few content slides from the most teachable passages
    slides = sorted(chunks, key=_teachable_score, reverse=True)[:3]
    flow = []
    per_slide = max(1, len(questions) // max(len(slides), 1))
    qi = 0
    for s in slides:
        flow.append({"type": "content", "title": s[:60] + ("..." if len(s) > 60 else ""),
                     "body": s[:320], "speak": s[:200]})
        for _ in range(per_slide):
            if qi < len(questions):
                flow.append({"type": "question", "question_id": questions[qi]["id"]})
                qi += 1
    while qi < len(questions):
        flow.append({"type": "question", "question_id": questions[qi]["id"]})
        qi += 1

    return {
        "id": lesson_id, "title": title, "subject": subject, "grade": grade,
        "topic_label": title, "duration_min": 20,
        "greeting": f"Hello class! I am CAT. Today's lesson comes straight from your "
                    f"textbook: {title}. Let's begin!",
        "closing": "Wonderful work today! You practiced your textbook like champions!",
        "source_pdf": Path(str(pdf_path)).name,
        "flow": flow,
        "questions": questions,
        "knowledge_base": kb,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--title", required=True)
    ap.add_argument("--subject", default="Mathematics")
    ap.add_argument("--grade", default="Grade 3")
    ap.add_argument("--id", dest="lesson_id", required=True)
    ap.add_argument("--questions", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()

    lesson = build_lesson(args.pdf, args.lesson_id, args.title, args.subject, args.grade,
                          args.questions, args.max_pages, progress=print)
    out = LESSONS_DIR / f"{args.lesson_id}.json"
    out.write_text(json.dumps(lesson, indent=2), encoding="utf-8")
    from . import database as db
    db.init_db()
    db.upsert_lesson(lesson)
    print(f"\nLesson saved: {out}  ({len(lesson['questions'])} questions, "
          f"{len(lesson['knowledge_base'])} KB passages)")
