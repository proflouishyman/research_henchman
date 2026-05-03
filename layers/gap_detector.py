"""Multi-pass gap detector.

Three passes shipped:

  Pass A — *intro-promise extraction*. The Introduction of a manuscript
           promises a set of topics, regions, named entities, or thesis
           claims that the reader expects later chapters to flesh out.
           We extract these with an LLM (with explicit category coverage
           and few-shot examples), then deterministically pair each
           promise to a real heading in the manuscript and decide whether
           that section is *empty* (tier 1, big gap) or *thin* (tier 2,
           small gap). Promises matched to a well-developed section
           (>300 words) are dropped — already covered. Promises whose
           best heading match scores below 0.5 are kept as gaps with a
           NULL chapter (``unmatched_heading``) — the missing-section
           signal is itself useful, just without a destination.

  Pass B — *bracketed TODO extraction*. Pure regex over the body of the
           manuscript surfaces author annotations like
           ``[need section on X]``. An LLM classifier then splits each
           bracketed string into one of two lanes:
             - ``research_gap`` — actual topic that needs a literature
               pull (default tier-1, status pending)
             - ``editorial_todo`` — note-to-self about prose / structure
               that does NOT need new sources (tier-3, status rejected)
           Both stay in the DB for transparency; only research_gap rows
           are pull-eligible.

  Pass F — *company / character profile gaps*. Walks the manuscript for
           named companies, people, regulatory frameworks and decides
           which deserve a comprehensive multi-source pull (10-Ks,
           business press, trade magazines, scholarly histories). Three
           signals: dedicated heading, body-mention frequency, intro
           presence. The classifier emits CP1, CP2, … gaps with
           gap_type='company_profile'.

All three functions return ``List[Dict]`` ready to feed into
``adapters.gap_tree.insert_node`` — they do not write to the DB themselves.
Pass B additionally calls ``update_gap_classification`` on rows that
already exist (resume-safe).

Helpers from ``layers.analysis`` are reused (``_extract_docx``,
``_split_sections``); that module is not modified.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from layers.analysis import _extract_docx, _split_sections


# ---------------------------------------------------------------------------
# Pass A — Intro-promise extraction
# ---------------------------------------------------------------------------

PASS_A_SYSTEM_PROMPT = """\
You are analyzing the Introduction of a history-of-e-commerce manuscript.

GOAL: extract a comprehensive list of EVERY topical promise the introduction
makes — every theme, region, company, person, regulation, statistic, and
historical thread that the rest of the book is expected to develop. Aim for
20–40 promises. Err on the side of MORE rather than fewer. A promise can be
implicit ("China became the world's largest retail market") or explicit
("we will see how Amazon ..."). If the intro mentions an entity at all in a
way that signals "this matters for what follows", it is a promise.

EXTRACT EVERY DIFFERENT KIND OF PROMISE, including:

1. **Named regions / national markets** the intro flags as important
   (China, India, Africa, Latin America, South America, Europe, US, Russia).
   EACH region = ONE promise of its own, even if mentioned briefly.
2. **Named companies / platforms** (Amazon, eBay, Mercado Libre, Shein,
   Temu, Alibaba, Flipkart, Tmall, JD.com, FedEx, UPS, Sears, Wal-Mart,
   Microsoft, Apple, Google, IBM, Netscape, AOL, Yahoo, PayPal, Confinity,
   Mosaic, NetMarket, ISN, HSN, Dell, Cisco, etc.). One promise per
   company even when several are named in the same sentence.
3. **Specific quantitative claims** ("China = 24% of world retail
   e-commerce", "two-thirds of humans online via smartphone", "65% of B2B").
4. **Named historical actors / people** ("Sears Roebuck on the telegraph",
   "Wal-Mart logistics under Sam Walton", "Bezos at Amazon", "Jack Ma at
   Alibaba", "Pierre Omidyar at eBay", "Magaziner Framework architect").
5. **Thematic threads / arguments** ("trust as an ancient problem",
   "long-distance trade in medieval Europe", "workshop China and shopaholic
   America", "platform cooperativists vs monopolies", "warehouse labor
   organizing", "crypto activists undermining government regulation").
6. **Promised future-chapter signals** — phrases like "we will see",
   "this book argues", "the rest of this book", "to understand X requires",
   "later we will", "the following chapters". Whatever follows such a
   phrase is a promise.
7. **Named regulations / legal frameworks** ("Magaziner Framework",
   "Taft-Hartley", "demilitarization of encryption", "Section 230", "Telecom
   Act of 1996").

OUTPUT SCHEMA — one JSON array of objects, no prose, no fences.
Each object must have:
- promise_text: the verbatim sentence or phrase from the intro (≤200 chars)
- key_entity: the most specific named subject (company, person, place,
              regulation, named theme). If multiple candidates, pick the
              one a librarian would search by.
- region: "US" | "China" | "India" | "Latin America" | "Europe" | "Africa"
          | "Global"
- expected_chapter_hint: 3-6 word guess at which chapter would cover this
- importance: 1-5 (5 = central thesis claim, 1 = passing reference)

WORKED EXAMPLES (study these — your output should look like this):

Example 1 — sentence: "China became the world's largest retail market and
accounted for nearly half of all global e-commerce by 2020."
→ {
    "promise_text": "China became the world's largest retail market and accounted for nearly half of all global e-commerce by 2020.",
    "key_entity": "China e-commerce market",
    "region": "China",
    "expected_chapter_hint": "Chinese Characteristics chapter",
    "importance": 5
  }

Example 2 — sentence: "Mercado Libre rose to dominate Latin American
online retail."
→ {
    "promise_text": "Mercado Libre rose to dominate Latin American online retail.",
    "key_entity": "Mercado Libre",
    "region": "Latin America",
    "expected_chapter_hint": "Latin America platforms",
    "importance": 4
  }

Example 3 — sentence: "Two-thirds of all humans now access the internet
through a smartphone, transforming what online shopping looks like."
→ {
    "promise_text": "Two-thirds of all humans now access the internet through a smartphone.",
    "key_entity": "smartphone internet adoption",
    "region": "Global",
    "expected_chapter_hint": "E-Commerce On The Go",
    "importance": 4
  }

Now apply the same approach to the Introduction below. Return 20-40 items.
"""

PASS_B_RQ_SYSTEM_PROMPT = """\
You are converting an author's terse TODO into a clear research question
that a librarian can use. Output ONE concise sentence."""


PASS_B_CLASSIFY_SYSTEM_PROMPT = """\
You classify bracketed annotations from a manuscript draft. The author writes
two distinct kinds of bracketed notes:

(a) RESEARCH_GAP — the note describes a TOPIC that needs new sources pulled
    from the library (10-Ks, scholarly articles, newspapers, books). Hints:
    names a subject ("Alipay history"), names a person/place/company, asks a
    factual question, mentions data needed, asks for backstory.

(b) EDITORIAL_NOTE — the note is about PROSE/STRUCTURE only and needs no new
    sources. Hints: tells the author to sharpen, cut, build, transition,
    re-organize; says "this can be sharper", "build on chapter X", "describe
    further", "more on this please", "stunningly brilliant subconclusion",
    "BUILD ON CHAPTER TWO", "this chapter is way too sprawling", "need
    linking comparisons".

Output STRICT JSON:
  {"classification": "research_gap" | "editorial_note",
   "confidence": 0.0-1.0,
   "reason": "<one short sentence>"}
No prose, no fences."""


PASS_F_RQ_SYSTEM_PROMPT = """\
You are commissioning a research dossier for a historian writing about an
entity. The dossier needs to enable a chapter-length narrative covering:
corporate history (founding, IPO, key strategic shifts, financials), key
people, market position, controversies, and broader cultural significance.
Phrase the research target as ONE sentence. No markdown, no list."""


# Stop-words used by the cheap TF-IDF-style scorer in heading pairing.
_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "but", "by", "for", "if", "in",
    "into", "is", "it", "its", "of", "on", "or", "that", "the", "to",
    "was", "were", "with", "from", "this", "these", "those", "have",
    "has", "had", "are", "we", "you", "i", "our", "their", "his", "her",
    "they", "she", "he", "do", "does", "did", "not", "no", "yes",
    "chapter", "section",
}


def _tokenize(text: str) -> List[str]:
    """Lowercase + alphanumeric + drop stopwords for pairing scorer."""
    if not text:
        return []
    raw = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return [w for w in raw if w not in _STOPWORDS and len(w) > 2]


def _extract_introduction(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return (introduction_text, all_sections).

    The Introduction is the first section whose heading contains
    "Introduction" (case-insensitive). The section ends at the next heading
    whose text starts with "Chapter" — or at the next section, whichever
    comes first if the manuscript has no chapter markers (e.g. tests).
    """
    sections = _split_sections(text)

    intro_idx: Optional[int] = None
    for i, sec in enumerate(sections):
        heading = sec.get("heading", "")
        if "introduction" in heading.lower():
            intro_idx = i
            break

    if intro_idx is None:
        return "", sections

    accum_lines: List[str] = list(sections[intro_idx].get("lines", []))
    for j in range(intro_idx + 1, len(sections)):
        next_heading = sections[j].get("heading", "")
        if next_heading.lower().startswith("chapter"):
            break
        accum_lines.extend(sections[j].get("lines", []))

    intro_text = " ".join(accum_lines).strip()
    return intro_text, sections


def _heading_word_counts(sections: List[Dict[str, Any]]) -> Dict[str, int]:
    """Return {heading: body_word_count} for every section."""
    out: Dict[str, int] = {}
    for sec in sections:
        heading = sec.get("heading", "")
        body = " ".join(sec.get("lines", [])).strip()
        out[heading] = len(body.split())
    return out


# ---------------------------------------------------------------------------
# Heading-pairing helpers (pseudo-heading filter)
# ---------------------------------------------------------------------------

# Pseudo-headings that should never be a promise's destination chapter.
_PSEUDO_HEADINGS = {
    "manuscript body",
    "(no heading)",
    "introduction",   # the intro itself — promises always point forward
    "conclusion",     # plain "conclusion" sub-headings appear in many chapters
}


def _is_pseudo_heading(heading: str, body_word_count: int) -> bool:
    """True if this heading is structural noise rather than a real chapter.

    Excluded:
      - blank headings
      - the placeholder "Manuscript Body" / "(no heading)"
      - the manuscript's Introduction itself (promises always point forward)
      - bracketed TODO-shaped headings like ``[NEED CONCLUSION FOR FIRST
        SECTION]`` — those are author notes, not destinations.

    NOT excluded: real chapter heads whose body happens to be empty or
    very short — those are exactly the *interesting* targets for
    intro-promise pairing (an empty Chapter 4 is the gap).
    """
    if not heading:
        return True
    h = heading.strip()
    if h.lower() in _PSEUDO_HEADINGS:
        return True
    # Bracketed TODO-shaped headings are author-notes, not real chapters.
    if h.startswith("[") and h.endswith("]"):
        return True
    return False


def _score_heading_for_promise(
    promise_text: str,
    key_entity: str,
    region: str,
    heading: str,
) -> float:
    """Deterministic similarity score between a promise and a heading.

    Score components:
      +2.0  substring match of *key_entity* (case-insensitive) in heading
      +1.0  substring match of *region* in heading
      0..+1 cheap word-overlap of promise_text vs heading

    The result is in roughly [0, 4]. Caller picks the top-scoring heading;
    callers below also enforce a minimum-score threshold of 0.5 for
    "match counts as paired" — anything weaker becomes ``unmatched``.
    """
    score = 0.0
    h_low = heading.lower().strip()

    if key_entity and key_entity.strip().lower() in h_low:
        score += 2.0

    if region and region.strip().lower() in h_low and region.strip().lower() != "global":
        score += 1.0

    p_tokens = Counter(_tokenize(promise_text))
    h_tokens = Counter(_tokenize(heading))
    if p_tokens and h_tokens:
        overlap = sum((p_tokens & h_tokens).values())
        denom = max(1, math.sqrt(sum(p_tokens.values()) * sum(h_tokens.values())))
        score += min(1.0, overlap / denom)

    return score


def _classify_promise_tier(body_word_count: int) -> Optional[Tuple[int, int]]:
    """Decide tier and evidence_target from how developed the section is.

    Returns (tier, evidence_target), or None if the section is already
    well-developed and the promise should be SKIPPED.
    """
    if body_word_count < 80:
        return (1, 120)
    if body_word_count <= 300:
        return (2, 60)
    return None  # >300 words → already covered


def _research_question_for_promise(
    promise: Dict[str, Any],
    matched_heading: str,
    llm: Any,
) -> str:
    """Ask the LLM for a one-sentence research question for an intro promise.

    Falls back to the verbatim promise_text if the LLM call errors or
    returns nothing useful.
    """
    promise_text = (promise.get("promise_text") or "").strip()
    key_entity = (promise.get("key_entity") or "").strip()
    if not promise_text:
        return ""
    user = (
        f"Promise from the manuscript Introduction:\n  \"{promise_text}\"\n"
        f"Key entity: {key_entity or '(none)'}\n"
        f"Expected chapter: {matched_heading or '(unmatched)'}\n\n"
        "Convert this promise into ONE concise research question a "
        "librarian could use to find scholarly sources. Reply with only "
        "the question."
    )
    try:
        out = llm.complete(system=PASS_B_RQ_SYSTEM_PROMPT, prompt=user, temperature=0.1)
        return re.sub(r"\s+", " ", (out or "").strip()).strip('"').strip()[:400]
    except Exception:
        return promise_text[:400]


def detect_pass_a(
    docx_path: Path,
    llm: Any,
    *,
    formatter_llm: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Pass A: extract intro promises and classify them as tier-1/tier-2 gaps.

    Returns a list of dicts ready for ``adapters.gap_tree.insert_node``:

        {
          "gap_id":           "IP1",
          "tier":             1 or 2,
          "gap_type":         "intro_promise",
          "chapter":          <matched heading> or None (unmatched),
          "heading_path":     <matched heading> or None,
          "claim_text":       <verbatim promise sentence>,
          "research_question":<LLM-derived 1-sentence question>,
          "source_locator":   "introduction",
          "evidence_target":  120 or 60,
          "detector_pass":    "A",
          "rationale":        "<explanation of why kept>",
        }

    Returns an empty list if the manuscript has no Introduction or the
    LLM produces no parseable promises.
    """
    from scripts.score_relevance import repair_json_with_fallback  # noqa: WPS433

    text = _extract_docx(Path(docx_path))
    intro_text, sections = _extract_introduction(text)
    if not intro_text:
        return []

    user_prompt = (
        "Manuscript Introduction (verbatim):\n\n"
        f"{intro_text[:12000]}\n\n"
        "Return the JSON array of promises now. Aim for 20-40 items."
    )

    parsed: Any = None
    try:
        parsed = llm.complete_json(
            system=PASS_A_SYSTEM_PROMPT,
            prompt=user_prompt,
            temperature=0.1,
        )
    except Exception:
        parsed = None

    if not isinstance(parsed, list):
        try:
            raw = llm.complete(
                system=PASS_A_SYSTEM_PROMPT,
                prompt=user_prompt,
                temperature=0.1,
            )
        except Exception:
            raw = ""
        parsed = repair_json_with_fallback(raw or "", formatter_llm) or []

    if not isinstance(parsed, list) or not parsed:
        return []

    word_counts = _heading_word_counts(sections)

    # Real headings only — pseudo-headings such as "Manuscript Body",
    # bracketed-TODO headings, and headings whose body is shorter than the
    # heading itself never become a promise's destination.
    real_headings: List[str] = [
        h for h, wc in word_counts.items()
        if not _is_pseudo_heading(h, wc)
    ]

    out: List[Dict[str, Any]] = []
    ip_index = 0

    for promise in parsed:
        if not isinstance(promise, dict):
            continue
        promise_text = (promise.get("promise_text") or "").strip()
        if not promise_text:
            continue
        key_entity = (promise.get("key_entity") or "").strip()
        region = (promise.get("region") or "").strip()

        # Pair with the best-matching real heading.
        best_heading: Optional[str] = None
        best_score = -1.0
        for h in real_headings:
            sc = _score_heading_for_promise(promise_text, key_entity, region, h)
            if sc > best_score:
                best_score = sc
                best_heading = h

        # Apply the pairing-confidence threshold. Anything below 0.5 is
        # treated as unmatched — the gap is still tier-1 (the missing
        # destination is itself the signal) but chapter is NULL so the
        # review file flags it specially.
        matched: bool = (best_heading is not None) and (best_score >= 0.5)

        if matched:
            assert best_heading is not None
            body_wc = word_counts.get(best_heading, 0)
            cls = _classify_promise_tier(body_wc)
            if cls is None:
                # Section already developed — skip per spec.
                continue
            tier, evidence_target = cls
            chapter_for_node: Optional[str] = best_heading
            heading_path_for_node: Optional[str] = best_heading
            rationale_extras = (
                f"Pairing score={best_score:.2f}; "
                f"matched section body={body_wc} words"
            )
        else:
            # Unmatched promise — still a tier-1 gap (no destination = big
            # missing chunk). Use evidence_target=120 like other empty-section
            # gaps. Chapter is NULL so the review file can flag it.
            tier = 1
            evidence_target = 120
            chapter_for_node = None
            heading_path_for_node = None
            rationale_extras = (
                f"unmatched_heading; best_score={max(0.0, best_score):.2f}"
            )

        ip_index += 1
        gap_id = f"IP{ip_index}"

        rq = _research_question_for_promise(
            promise={"promise_text": promise_text, "key_entity": key_entity},
            matched_heading=chapter_for_node or "",
            llm=llm,
        )

        rationale = rationale_extras
        if promise.get("importance"):
            rationale += f"; importance={promise.get('importance')}"

        out.append({
            "gap_id":            gap_id,
            "tier":              tier,
            "gap_type":          "intro_promise",
            "chapter":           chapter_for_node,
            "heading_path":      heading_path_for_node,
            "claim_text":        promise_text[:600],
            "research_question": rq,
            "source_locator":    "introduction",
            "evidence_target":   evidence_target,
            "detector_pass":     "A",
            "rationale":         rationale,
        })

    return out


# ---------------------------------------------------------------------------
# Pass B — Bracketed TODO extraction + research/editorial classifier
# ---------------------------------------------------------------------------

# Tolerate leading whitespace before the first letter; manuscripts have
# bracketed TODOs like ``[       MORE ON THIS PLEASE]``.
_BRACKET_RE = re.compile(r"\[\s*([A-Za-z][^\]]{4,300})\]", re.DOTALL)


def _looks_like_citation(content: str) -> bool:
    """Filter out citation-style brackets like [Smith 2003] or [Bezos, 2013]."""
    words = content.split()
    if len(words) <= 4 and re.search(r"\b(18|19|20)\d{2}\b", content):
        return True
    return False


def _evidence_target_for_todo(content: str) -> int:
    """40 if the TODO is ≤6 words (narrow gap), 80 otherwise."""
    n_words = len(content.split())
    return 40 if n_words <= 6 else 80


def _classify_todo(content: str, llm: Any) -> Dict[str, Any]:
    """Classify a bracketed TODO as research_gap vs editorial_note.

    Returns a dict ``{classification, confidence, reason}``. On any failure
    or unparseable reply the safe default is ``research_gap`` with a low
    confidence — this preserves the prior behavior (every TODO is a gap)
    when the classifier is unavailable, and the user can always reject in
    the review file.
    """
    if llm is None:
        return {"classification": "research_gap", "confidence": 0.0,
                "reason": "no classifier llm provided"}
    user = (
        f"Bracketed annotation:\n  \"{content}\"\n\n"
        "Classify and return JSON."
    )
    try:
        parsed = llm.complete_json(
            system=PASS_B_CLASSIFY_SYSTEM_PROMPT,
            prompt=user,
            temperature=0.0,
        )
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        return {"classification": "research_gap", "confidence": 0.0,
                "reason": "classifier parse failure"}
    cls = str(parsed.get("classification") or "").strip().lower()
    if cls not in {"research_gap", "editorial_note"}:
        cls = "research_gap"
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(parsed.get("reason") or "")[:200]
    return {"classification": cls, "confidence": conf, "reason": reason}


def detect_pass_b(
    docx_path: Path,
    *,
    llm: Optional[Any] = None,
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Pass B: regex over the manuscript body for bracketed TODOs, then
    classify each as ``research_gap`` (default tier-1, status pending) or
    ``editorial_todo`` (tier-3, status rejected).

    Returns dicts ready for ``insert_node``. If a *conn* is supplied, the
    function additionally:
      - reuses any existing research_question on disk (resume safety on
        re-runs — no LLM re-call), and
      - calls ``update_gap_classification`` on rows that already exist but
        whose gap_type is still the legacy ``explicit_todo``, demoting any
        editorial notes into the rejected lane.
    """
    from adapters.gap_tree import (  # noqa: WPS433
        fetch_gap_type,
        fetch_research_question,
        gap_exists,
        update_gap_classification,
    )

    text = _extract_docx(Path(docx_path))
    sections = _split_sections(text)

    todos: List[Tuple[str, str]] = []  # (chapter_heading, content)
    seen: set = set()
    last_real_chapter = "Manuscript Body"

    for sec in sections:
        heading = sec.get("heading", "Manuscript Body")
        body = " ".join(sec.get("lines", []))

        # Bracketed-shaped headings: the splitter promotes them to headings
        # because they're ALLCAPS and short. Route to the previous real
        # chapter as their chapter.
        head_match = _BRACKET_RE.match(heading)
        if head_match:
            content = head_match.group(1).strip()
            if (
                len(content) >= 10
                and " " in content
                and not _looks_like_citation(content)
            ):
                key = (last_real_chapter, content.lower())
                if key not in seen:
                    seen.add(key)
                    todos.append((last_real_chapter, content))
        else:
            last_real_chapter = heading

        for m in _BRACKET_RE.finditer(body):
            content = m.group(1).strip()
            if len(content) < 10:
                continue
            if " " not in content:
                continue
            if _looks_like_citation(content):
                continue
            key = (last_real_chapter, content.lower())
            if key in seen:
                continue
            seen.add(key)
            todos.append((last_real_chapter, content))

    out: List[Dict[str, Any]] = []
    for i, (heading, content) in enumerate(todos, start=1):
        gap_id = f"TODO{i}"
        evidence_target = _evidence_target_for_todo(content)

        # Resume-safe: if the row is already in the DB and has a
        # research_question, reuse it without calling the LLM.
        rq: Optional[str] = None
        existing_type: Optional[str] = None
        if conn is not None:
            rq = fetch_research_question(conn, gap_id)
            existing_type = fetch_gap_type(conn, gap_id)

        if not rq and llm is not None:
            try:
                user = (
                    f"Author's bracketed TODO from the manuscript:\n"
                    f"  \"{content}\"\n\n"
                    "Convert this terse note into ONE concise research "
                    "question that a librarian could use to find scholarly "
                    "sources. Reply with only the question."
                )
                resp = llm.complete(
                    system=PASS_B_RQ_SYSTEM_PROMPT,
                    prompt=user,
                    temperature=0.1,
                )
                rq = re.sub(r"\s+", " ", (resp or "").strip()).strip('"').strip()[:400]
            except Exception:
                rq = None
        if not rq:
            rq = content[:400]

        # Classify each TODO. If the row was previously inserted with a
        # terminal gap_type (research_gap or editorial_todo), skip the
        # classifier call to keep re-runs cheap.
        terminal_types = {"research_gap", "editorial_todo"}
        if existing_type in terminal_types:
            classification = (
                "editorial_note" if existing_type == "editorial_todo"
                else "research_gap"
            )
            confidence = 1.0
            reason = "preserved from prior run"
        else:
            res = _classify_todo(content, llm)
            classification = res["classification"]
            confidence = res["confidence"]
            reason = res["reason"]

        if classification == "editorial_note":
            gap_type = "editorial_todo"
            tier = 3
            status = "rejected"
        else:
            gap_type = "research_gap"
            tier = 1
            status = "pending"

        rationale = (
            f"bracketed TODO; {len(content.split())} words; "
            f"classifier={classification} (conf={confidence:.2f}): {reason}"
        )

        # If the row exists with the legacy ``explicit_todo`` gap_type, the
        # caller (build_gap_tree.py) will see gap_exists==True and skip the
        # insert — but we need the classification to land on disk. Update it
        # in place.
        if conn is not None and gap_exists(conn, gap_id) and existing_type not in terminal_types:
            update_gap_classification(
                conn, gap_id,
                gap_type=gap_type,
                tier=tier,
                status=status,
                rationale=rationale,
            )

        out.append({
            "gap_id":            gap_id,
            "tier":              tier,
            "gap_type":          gap_type,
            "chapter":           heading,
            "heading_path":      heading,
            "claim_text":        content[:600],
            "research_question": rq,
            "source_locator":    "manuscript_body",
            "evidence_target":   evidence_target,
            "detector_pass":     "B",
            "status":            status,
            "rationale":         rationale,
        })

    return out


# ---------------------------------------------------------------------------
# Pass F — Company / character profile gaps
# ---------------------------------------------------------------------------

# Seed list of candidate companies/people/regulations the gap_detector
# considers as "main characters" if they actually appear in the manuscript.
# Anything not in the manuscript is filtered out at runtime — this list is a
# starting set, not a strict whitelist.
PASS_F_SEED_ENTITIES: List[str] = [
    # Big Tech / Internet pioneers
    "Amazon", "eBay", "Microsoft", "Apple", "IBM", "Google", "Yahoo",
    "Netscape", "AOL", "Dell", "Cisco", "PayPal", "Confinity", "Mosaic",
    "AWS",
    # Logistics
    "FedEx", "UPS",
    # Retailers
    "Sears", "Sears Roebuck", "Walmart", "Wal-Mart", "Fingerhut",
    "NetMarket", "ISN", "HSN", "QVC", "Buy.com", "Prodigy", "Blockbuster",
    "Netflix", "CommerceNet",
    # Payments
    "Visa", "Mastercard",
    # China
    "Alibaba", "Tmall", "Taobao", "Alipay", "Ant Group", "Tencent",
    "Baidu", "JD.com", "Pinduoduo", "Xiaomi", "Huawei",
    # India
    "Flipkart", "Snapdeal", "Paytm",
    # Latin America / global
    "Mercado Libre", "Shein", "Temu",
    # People
    "Bezos", "Jack Ma", "Pierre Omidyar", "Meg Whitman", "Magaziner",
    "Clinton", "Berners-Lee", "Omidyar",
    # Regulation
    "Federal Trade Commission", "PGP",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _entity_appears_in_text(entity: str, text_lower: str) -> int:
    """Return the number of case-insensitive occurrences of *entity* in text."""
    if not entity:
        return 0
    e = entity.lower()
    # Word-boundary regex when the entity is alphanumeric only; for
    # entities with punctuation (Wal-Mart, JD.com, Buy.com) fall back to a
    # plain substring count which preserves dots and dashes.
    if re.fullmatch(r"[a-z0-9 ]+", e):
        pat = re.compile(rf"\b{re.escape(e)}\b")
        return len(pat.findall(text_lower))
    return text_lower.count(e)


def _heading_targets_entity(heading: str, entity: str) -> bool:
    """True if a heading is dedicated to *entity*.

    Heuristic: the heading text equals the entity (case-insensitive,
    ignoring leading "Chapter N:" prefixes) OR contains the entity as a
    word and is short (<= 8 words).
    """
    if not heading or not entity:
        return False
    h = heading.strip()
    h_low = h.lower()
    e_low = entity.lower()

    # Strip a leading chapter-prefix like "Chapter 4: "
    h_stripped = re.sub(r"^(chapter|part|section)\s+[ivxlcdm0-9]+\s*[:\.\-]?\s*",
                        "", h_low).strip()

    if h_stripped == e_low:
        return True
    if h_low == e_low:
        return True
    # Short heading containing the entity name as a word.
    if len(h.split()) <= 8 and re.search(rf"\b{re.escape(e_low)}\b", h_low):
        return True
    return False


def _find_dedicated_section(
    sections: List[Dict[str, Any]],
    entity: str,
) -> Optional[Tuple[str, int]]:
    """Return (heading, body_word_count) of the section dedicated to *entity*,
    or None if no section is dedicated."""
    best: Optional[Tuple[str, int]] = None
    for sec in sections:
        h = sec.get("heading", "")
        if _heading_targets_entity(h, entity):
            wc = len(" ".join(sec.get("lines", [])).split())
            # If multiple matches, pick the longest body (most "dedicated").
            if best is None or wc > best[1]:
                best = (h, wc)
    return best


def _docx_heading_sections(docx_path: Path) -> List[Tuple[str, int]]:
    """Walk the docx with python-docx and return [(heading_text, body_word_count), ...]
    using Word's *style* metadata (Heading 1/2/3/Title) as the truth source.

    The legacy ``_split_sections`` uses regex over flattened text and misses
    headings that don't match its patterns (it returned 26 sections vs 94
    real docx headings on the live manuscript). This helper is used as a
    fallback so Pass F can detect entity-dedicated empty sections like
    "Mercado Libre", "Shein and Temu", "Tmall", and "Alipay and Its Copycats".
    """
    try:
        import docx as _docx  # type: ignore
    except ImportError:
        return []
    try:
        d = _docx.Document(str(docx_path))
    except Exception:
        return []
    out: List[Tuple[str, int]] = []
    current_heading: Optional[str] = None
    body_chars: int = 0
    body_words: int = 0
    for p in d.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        is_heading = ("heading" in style) or ("title" in style)
        if is_heading:
            if current_heading is not None:
                out.append((current_heading, body_words))
            current_heading = t
            body_words = 0
        else:
            if current_heading is not None:
                body_words += len(t.split())
    if current_heading is not None:
        out.append((current_heading, body_words))
    return out


def _find_dedicated_section_docx(
    docx_sections: List[Tuple[str, int]],
    entity: str,
) -> Optional[Tuple[str, int]]:
    """Like ``_find_dedicated_section`` but operates on python-docx-walked
    sections (style-based, accurate). Used as a fallback when the legacy
    text-based splitter misses an entity-dedicated heading."""
    best: Optional[Tuple[str, int]] = None
    for h, wc in docx_sections:
        if _heading_targets_entity(h, entity):
            if best is None or wc > best[1]:
                best = (h, wc)
    return best


def _research_question_for_company(entity: str, llm: Any) -> str:
    """One-sentence research-target line for a company-profile dossier."""
    if llm is None:
        return (f"Compile a research dossier covering {entity}: corporate "
                f"history, key people, market position, controversies, and "
                f"cultural significance.")
    user = f"Entity: {entity}\n\nWrite the one-sentence research target."
    try:
        out = llm.complete(
            system=PASS_F_RQ_SYSTEM_PROMPT,
            prompt=user,
            temperature=0.1,
        )
        cleaned = re.sub(r"\s+", " ", (out or "").strip()).strip('"').strip()
        return cleaned[:400] or (
            f"Compile a research dossier covering {entity}."
        )
    except Exception:
        return f"Compile a research dossier covering {entity}."


def detect_pass_f(
    docx_path: Path,
    *,
    llm: Optional[Any] = None,
    conn: Optional[Any] = None,
    entity_seeds: Optional[Iterable[str]] = None,
    extra_entities: Optional[Iterable[str]] = None,
    min_body_mentions: int = 5,
    include_covered: bool = False,
) -> List[Dict[str, Any]]:
    """Pass F: company / character profile gaps.

    Walks the manuscript for each candidate entity (seed list +
    *extra_entities*, e.g. Pass A's key_entity values) and emits a CP gap
    for any entity that:
      - has a dedicated heading + body < 200 words (tier 1, evidence 200,
        rationale 'empty section')
      - has no dedicated heading + body_mention_count >= min_body_mentions
        + is referenced in the intro (tier 1, evidence 150,
        rationale 'no dedicated section')
      - has a dedicated heading + 200 ≤ body ≤ 800 words (tier 2,
        evidence 80, rationale 'thin section')
      - has a dedicated heading + body > 800 words → no gap, covered.

    *conn* is honored only for resume safety; the function does not write
    to the DB itself.
    """
    from adapters.gap_tree import gap_exists  # noqa: WPS433

    text = _extract_docx(Path(docx_path))
    text_lower = text.lower()
    sections = _split_sections(text)
    docx_sections = _docx_heading_sections(Path(docx_path))
    intro_text, _ = _extract_introduction(text)
    intro_lower = intro_text.lower()

    # Build the candidate set: seeds (those that actually appear) plus any
    # extras supplied by the caller (e.g. Pass A key_entity values).
    seeds = list(entity_seeds) if entity_seeds is not None else list(PASS_F_SEED_ENTITIES)
    if extra_entities:
        seeds.extend(extra_entities)

    # Dedupe by normalized form, preserve insertion order.
    seen_norm: Set[str] = set()
    candidates: List[str] = []
    for s in seeds:
        n = _norm(s)
        if not n or n in seen_norm:
            continue
        seen_norm.add(n)
        candidates.append(s.strip())

    out: List[Dict[str, Any]] = []
    cp_index = 0

    # Threshold above which body-mention count alone qualifies an entity as a
    # "main character" even if it never appears in the introduction. Set high
    # enough to filter out passing references but low enough to catch
    # body-only protagonists like Flipkart, Alibaba, and other later-chapter
    # subjects the intro doesn't preview.
    MAIN_CHARACTER_BODY_FLOOR = 10

    for entity in candidates:
        body_mentions = _entity_appears_in_text(entity, text_lower)
        if body_mentions == 0:
            continue

        # Try the legacy text-based splitter first; fall back to the
        # docx-style-aware splitter for entities whose dedicated heading
        # is missed by the regex-based splitter.
        dedicated = (
            _find_dedicated_section(sections, entity)
            or _find_dedicated_section_docx(docx_sections, entity)
        )
        in_intro = _entity_appears_in_text(entity, intro_lower) > 0

        if dedicated is not None:
            head, body_wc = dedicated
            if body_wc < 200:
                tier, ev, rationale = 1, 200, "empty section"
            elif body_wc <= 800:
                tier, ev, rationale = 2, 80, "thin section"
            elif include_covered:
                # User opted to pull supplementary evidence even for
                # already-drafted main characters (10-Ks, trade press,
                # scholarly histories). Lower target since coverage exists.
                tier, ev, rationale = 2, 60, "supplementary (covered)"
            else:
                # Well-developed section — skip.
                continue
            chapter = head
        else:
            # No dedicated heading. Two paths qualify as "main character":
            # (1) intro mentions AND ≥ min_body_mentions body mentions, or
            # (2) body_mentions ≥ MAIN_CHARACTER_BODY_FLOOR (no intro
            #     requirement — body-only protagonists like Flipkart/Alibaba).
            if (not in_intro or body_mentions < min_body_mentions) and \
               body_mentions < MAIN_CHARACTER_BODY_FLOOR:
                continue
            tier, ev, rationale = 1, 150, "no dedicated section"
            chapter = "(no section yet)"

        cp_index += 1
        gap_id = f"CP{cp_index}"

        # Resume-safe: if the row already exists, skip the LLM call for
        # research_question — caller will see gap_exists==True and skip
        # the insert anyway.
        if conn is not None and gap_exists(conn, gap_id):
            rq = ""  # value isn't used; the existing row stays put
        else:
            rq = _research_question_for_company(entity, llm)

        out.append({
            "gap_id":            gap_id,
            "tier":              tier,
            "gap_type":          "company_profile",
            "chapter":           chapter,
            "heading_path":      chapter,
            "claim_text":        entity,
            "research_question": rq,
            "source_locator":    "company_profile",
            "evidence_target":   ev,
            "detector_pass":     "F",
            "rationale":         (
                f"{rationale}; body_mentions={body_mentions}; "
                f"in_intro={'yes' if in_intro else 'no'}"
                + (f"; dedicated_body={dedicated[1]}w" if dedicated else "")
            ),
        })

    return out
