// TypeScript contracts mirroring backend contracts.py

export type RunStatus =
  | 'queued'
  | 'analyzing'
  | 'planning'
  | 'pulling'
  | 'ingesting'
  | 'fitting'
  | 'complete'
  | 'failed'
  | 'partial'

export interface RunRow {
  run_id: string
  manuscript_path: string
  status: RunStatus
  stage_detail?: string
  error?: string
  created_at: string
  updated_at: string
  gap_count?: number
  gap_map?: GapMap
  research_plan?: ResearchPlan
}

export interface GapMap {
  manuscript_path: string
  gaps: Gap[]
  explicit_count: number
  implicit_count: number
  analysis_method: string
}

export interface Gap {
  gap_id: string
  chapter: string
  claim_text: string
  gap_type: 'explicit' | 'implicit'
  priority: 'high' | 'medium' | 'low'
  suggested_queries: string[]
  source_text_excerpt: string
}

export interface ResearchPlan {
  gaps: PlannedGap[]
  plan_summary?: string
}

export interface PlannedGap {
  gap_id: string
  chapter: string
  claim_text: string
  gap_type: string
  priority: string
  claim_kind: string
  evidence_need: string
  route_confidence: number
  search_queries: string[]
  preferred_sources: string[]
  rationale?: string
  needs_review: boolean
  query_ladder?: AccordionLadder
}

export interface AccordionLadder {
  constrained: string
  contextual: string
  broad: string
  fallback: string
  primary_term: string
  synonym_ring?: SynonymRing
  claim_kind: string
  evidence_need: string
  generation_method: string
}

export interface SynonymRing {
  terminology_shifts: string[]
  institutional_names: string[]
  era_modifiers: string[]
  era_start?: number
  era_end?: number
}

export interface Event {
  event_id: string
  run_id: string
  stage: string
  status: string
  message: string
  meta?: Record<string, unknown>
  ts_utc: string
}

export interface GapPacket {
  gap_id: string
  chapter: string
  claim_text: string
  sources: SourcePacket[]
}

export interface SourcePacket {
  source_id: string
  source_type: string
  documents: LinkedDocument[]
}

export interface LinkedDocument {
  evidence_id: string
  source_id: string
  source_locator: string
  title: string
  excerpt: string
  quality_rank: 'high' | 'medium' | 'seed'
  quality_label: string
  anchor_url?: string
  blocked_reason?: string
  action_required?: string
}

export interface SignInTarget {
  source_id: string
  name: string
  url: string
}

export interface SignInResult {
  source_id: string
  name: string
  status: 'ok' | 'blocked' | 'unreachable'
  blocked_reason?: string
  action_required?: string
}

export interface Source {
  source_id: string
  name: string
  url?: string
  categories?: string[]
  claim_kinds?: string[]
  evidence_needs?: string[]
}

export interface Settings {
  library_system?: string
  llm_provider?: string
  browser_provider?: string
  [key: string]: unknown
}

export interface Manuscript {
  name: string
  path: string
}
