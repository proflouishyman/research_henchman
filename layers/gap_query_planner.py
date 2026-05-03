"""Per-gap-type query generator.

Takes a gap_tree node dict and returns the list of ``(query, source_id)``
tuples the dispatch layer should run. This module owns the routing
contract — adding a new source for a gap_type means editing only this
file plus the dispatch shim.

Routing rules (Wave 2):

  intro_promise          → hathitrust_fulltext, ebsco_api, proquest_us_newsstream
                           (2 queries each: broad + narrow)
  intro_promise tier 1   → ALSO proquest_international_newsstream (1 query)
  research_gap           → ebsco_api, hathitrust_fulltext (1 query each)
  company_profile        → sec_edgar_10k (entity-name "query"; 8 forms),
                           ebsco_api, hathitrust_fulltext,
                           proquest_us_newsstream (1 query per non-EDGAR source)
  editorial_todo         → skipped (no pull)

LLM defaults:
  - llama3.1:8b for query rewrites (fast, structured, terse)

Quality concerns (per AGENTS.md): empty / oversized queries are clipped
to safe lengths, never fed unbounded LLM output to a Boolean parser.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Source identifiers (must match the IDs used by article_index source columns
# and dispatch shims). Keep these in sync with layers/pull_dispatch.py.
# ---------------------------------------------------------------------------

SRC_EBSCO          = "ebsco_api"
SRC_HATHI          = "hathitrust_fulltext"
SRC_PQ_US          = "proquest_us_newsstream"
SRC_PQ_INTL        = "proquest_international_newsstream"
SRC_SEC_10K        = "sec_edgar_10k"

# Gap types we know how to handle — anything else is dropped silently.
_PULLABLE_GAP_TYPES = {"intro_promise", "research_gap", "company_profile"}


# ---------------------------------------------------------------------------
# System prompts (per gap_type)
# ---------------------------------------------------------------------------

INTRO_PROMISE_SYSTEM = """\
You are commissioning a focused literature pull for a historian writing
about a topic the introduction of their manuscript promises to develop.

Generate a Boolean search query that will find scholarly + historical
sources on the named topic. Use AND/OR plus quoted phrases. The query
should be 60-120 characters — long enough to be specific, short enough
to keep recall up.

Output: a SINGLE query, one line. No commentary, no markdown, no
"Query:" prefix. Just the Boolean string.

Example:
Topic: "China became the world's largest retail market"
Query: ("China" OR "Chinese") AND ("e-commerce" OR "online retail" OR "online shopping") AND retail

Topic: "Mercado Libre dominated Latin American online retail"
Query: ("Mercado Libre" OR "MercadoLibre") AND ("Latin America" OR "Argentina" OR "Brazil") AND (retail OR e-commerce)
"""

RESEARCH_GAP_SYSTEM = """\
You are commissioning a tight research pull for a manuscript gap.
Generate ONE compact Boolean query, 40-80 characters. Use AND/OR plus
quoted phrases when needed.

Output: single query, one line, no commentary.

Example:
Gap: "Alipay regulatory history"
Query: "Alipay" AND (regulation OR PBOC OR licensing)

Gap: "ProQuest history of department store credit"
Query: "department store" AND (credit OR "charge account")
"""

COMPANY_PROFILE_SYSTEM = """\
You are commissioning trade-press / scholarly coverage of a single
company. Generate ONE Boolean query of 60-120 characters. Include
SYNONYM / ALIAS variants (e.g. "Wal-Mart" OR "Walmart"). Quote multi-
word names. Add 1-2 generic terms the source databases will treat as
relevant filters: "history", "company", "strategy", "founder", etc.

Output: single query, one line, no commentary.

Example:
Company: "Wal-Mart"
Query: ("Wal-Mart" OR "Walmart") AND (history OR strategy OR founder OR retail)

Company: "Mercado Libre"
Query: ("Mercado Libre" OR "MercadoLibre" OR "MELI") AND (history OR strategy OR e-commerce)
"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


def _clean_query(raw: str, *, max_len: int = 200) -> str:
    """Normalise an LLM response into a single-line query.

    Drops markdown fences, "Query:" prefixes, leading numbering, blank
    lines. Returns '' when the response is unusable so callers can fall
    back to the claim text directly.
    """
    if not raw:
        return ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        line = re.sub(
            r"^\s*(?:\d+[.):\s]+|Query[:\s]+|Q[:\s]+)",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        # Drop surrounding quotes so downstream Boolean parsers don't see
        # the LLM's "wrapper" quotes.
        if (line.startswith('"') and line.endswith('"')) or (
            line.startswith("'") and line.endswith("'")
        ):
            line = line[1:-1].strip()
        if line:
            return line[:max_len]
    return ""


def _llm_query(system: str, user_msg: str, llm: Any) -> str:
    """Wrap LLM call with the same defensive logging used by other pullers."""
    try:
        raw = llm.complete(system=system, prompt=user_msg, temperature=0.2)
    except Exception as exc:  # pragma: no cover — defensive
        return ""
    return _clean_query(raw)


# ---------------------------------------------------------------------------
# Per-gap-type planners
# ---------------------------------------------------------------------------


def _plan_intro_promise(node: Dict[str, Any], llm: Any) -> List[Tuple[str, str]]:
    """Hath + EBSCO + ProQuest US (broad + narrow each); +Intl on tier 1."""
    claim = (node.get("claim_text") or "").strip()
    if not claim:
        return []

    # Broad query — wide recall, generic concept terms.
    broad_user = f"Topic: \"{claim}\"\nGoal: BROAD recall — wide concept words."
    broad_q = _llm_query(INTRO_PROMISE_SYSTEM, broad_user, llm) or claim[:120]

    # Narrow query — specific named entities only.
    narrow_user = (
        f"Topic: \"{claim}\"\n"
        "Goal: NARROW precision — focus on named entities, dates, proper nouns."
    )
    narrow_q = _llm_query(INTRO_PROMISE_SYSTEM, narrow_user, llm) or claim[:80]

    plans: List[Tuple[str, str]] = []
    for src in (SRC_HATHI, SRC_EBSCO, SRC_PQ_US):
        plans.append((broad_q, src))
        if narrow_q and narrow_q != broad_q:
            plans.append((narrow_q, src))

    if int(node.get("tier", 2)) == 1:
        # Tier-1 build-from-scratch promises also get an Intl Newsstream pass
        # — many such promises are about non-US regions that US Newsstream
        # under-covers. Single query = the broad one.
        plans.append((broad_q, SRC_PQ_INTL))

    return plans


def _plan_research_gap(node: Dict[str, Any], llm: Any) -> List[Tuple[str, str]]:
    """Single tight query × EBSCO + HathiTrust."""
    claim = (node.get("claim_text") or "").strip()
    if not claim:
        return []

    user = f"Gap: \"{claim}\"\nQuery:"
    q = _llm_query(RESEARCH_GAP_SYSTEM, user, llm) or claim[:80]
    return [(q, SRC_EBSCO), (q, SRC_HATHI)]


def _plan_company_profile(node: Dict[str, Any], llm: Any) -> List[Tuple[str, str]]:
    """SEC EDGAR (entity name = "query") + 1 LLM-rewritten query × press sources."""
    entity = (node.get("claim_text") or "").strip()
    if not entity:
        return []

    user = f"Company: \"{entity}\"\nQuery:"
    press_q = _llm_query(COMPANY_PROFILE_SYSTEM, user, llm) or entity

    return [
        # SEC EDGAR puller treats the "query" as the entity name (it does
        # the CIK lookup itself); the dispatcher uses limit=8.
        (entity, SRC_SEC_10K),
        (press_q, SRC_EBSCO),
        (press_q, SRC_HATHI),
        (press_q, SRC_PQ_US),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_queries(node: Dict[str, Any], llm: Any) -> List[Tuple[str, str]]:
    """Return [(query, source_id), …] for a gap_tree node.

    *node* is expected to be a sqlite3.Row or a plain dict carrying at
    minimum: gap_type, claim_text, tier. Unknown gap_types and
    ``editorial_todo`` rows return an empty list (caller skips the gap).
    """
    gap_type = (node.get("gap_type") or "").strip()
    if gap_type not in _PULLABLE_GAP_TYPES:
        return []

    if gap_type == "intro_promise":
        return _plan_intro_promise(node, llm)
    if gap_type == "research_gap":
        return _plan_research_gap(node, llm)
    if gap_type == "company_profile":
        return _plan_company_profile(node, llm)
    return []
