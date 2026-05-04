// TypeScript contracts for the writing-companion library API.
//
// Mirrors the Pydantic models in ``contracts.py`` (LibraryGapsOut,
// LibraryDossierOut, LibraryIndexOut). Backend is the source of truth —
// when changing field names update both files.

export interface GapTreeRow {
  gap_id: string
  parent_gap_id: string | null
  depth: number
  tier: number | null
  gap_type: string
  chapter: string
  heading_path: string
  claim_text: string
  research_question: string
  source_locator: string
  evidence_target: number
  detector_pass: string
  status: string
  rationale: string
  created_at: string
  total_rows: number
  tier_counts: Record<string, number>  // keys: "3"|"2"|"1"|"0"|"unscored"
}

export interface DossierEntry {
  id: number
  title: string
  authors: string
  pub_date: string
  journal: string
  abstract: string
  doi: string
  url: string
  pdf_path: string
  source_id: string
  also_in_sources: string[]
  relevance_score: number | null
  relevance_why: string
  cross_gap_refs: string[]
}

export interface DossierSummary {
  total_rows: number
  consolidated: number
  tier_counts: Record<string, number>
}

export interface GapHeader {
  gap_id: string
  chapter: string
  claim_text: string
  research_question: string
  evidence_target: number
  tier: number | null
  gap_type: string
  status: string
  detector_pass: string
  rationale: string
  heading_path: string
}

export interface LibraryDossier {
  gap: GapHeader
  summary: DossierSummary
  tiers: Record<string, DossierEntry[]>  // keys: "3"|"2"|"1"|"0"|"unscored"
}

export interface LibraryChapter {
  slug: string
  title: string
  gap_count: number
  gaps: GapTreeRow[]
}

export interface LibraryIndex {
  chapters: LibraryChapter[]
  corpus_total_rows: number
  corpus_scored_rows: number
  sources: string[]
}

// ---------------------------------------------------------------------------
// Wave 2: corpus search + characters dashboard contracts.
// ---------------------------------------------------------------------------

/** One search result row — DossierEntry plus FTS context. */
export interface SearchResult extends DossierEntry {
  /** Owning gap_id (search results aren't pre-grouped by gap). */
  gap_id: string
  /** 200-char excerpt with ``<mark>`` tags around hits. */
  snippet: string
}

export interface LibrarySearch {
  total: number
  results: SearchResult[]
}

/** Filters posted to ``/api/library/articles/search``. */
export interface SearchFilters {
  sourceIds: string[]
  scoreMin: number
  gapId: string
  yearFrom: number | null
  yearTo: number | null
  hasPdf: boolean | null
}

/** One company-profile gap with character-card preview fields. */
export interface CharacterCard extends GapTreeRow {
  top_tier3_titles: string[]
  tier_histogram: Record<string, number>
}

export interface LibraryCharacters {
  characters: CharacterCard[]
}
