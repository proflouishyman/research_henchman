// Typed fetch wrappers for the writing-companion library API.
// Mirrors the conventions of ``lib/api.ts``.

import type {
  LibraryCharacters,
  LibraryDossier,
  LibraryIndex,
  LibrarySearch,
  GapTreeRow,
  SearchFilters,
  ManuscriptStructure,
  MarkRow,
} from '../types/library'

const BASE = '/api/library'

async function libFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`API ${path} → ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

export async function fetchLibraryIndex(): Promise<LibraryIndex> {
  return libFetch<LibraryIndex>('/index')
}

export async function fetchLibraryGaps(params: {
  chapter?: string
  gap_type?: string
  tier?: number
  status?: string
} = {}): Promise<GapTreeRow[]> {
  const query = new URLSearchParams()
  if (params.chapter) query.set('chapter', params.chapter)
  if (params.gap_type) query.set('gap_type', params.gap_type)
  if (params.tier !== undefined) query.set('tier', String(params.tier))
  if (params.status) query.set('status', params.status)
  const qs = query.toString()
  const data = await libFetch<{ gaps: GapTreeRow[] }>(`/gaps${qs ? '?' + qs : ''}`)
  return data.gaps
}

export async function fetchDossier(gapId: string): Promise<LibraryDossier> {
  return libFetch<LibraryDossier>(`/gaps/${encodeURIComponent(gapId)}/dossier`)
}

/** Build the URL for the existing file-serving endpoint (used to open PDFs). */
export function fileUrl(absoluteOrRepoRelativePath: string): string {
  return `/api/orchestrator/files?path=${encodeURIComponent(absoluteOrRepoRelativePath)}`
}

// ---------------------------------------------------------------------------
// Wave 2 — corpus search + characters dashboard
// ---------------------------------------------------------------------------

/** Search the corpus via FTS5. Returns ``{total, results}``. */
export async function searchArticles(
  q: string,
  filters: SearchFilters,
  opts: { limit?: number; offset?: number } = {},
): Promise<LibrarySearch> {
  const params = new URLSearchParams()
  params.set('q', q)
  if (filters.sourceIds.length > 0) {
    params.set('source_id', filters.sourceIds.join(','))
  }
  if (filters.scoreMin > 0) params.set('score_min', String(filters.scoreMin))
  if (filters.gapId) params.set('gap_id', filters.gapId)
  if (filters.yearFrom !== null) params.set('year_from', String(filters.yearFrom))
  if (filters.yearTo !== null) params.set('year_to', String(filters.yearTo))
  if (filters.hasPdf !== null) params.set('has_pdf', filters.hasPdf ? 'true' : 'false')
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  if (opts.offset !== undefined) params.set('offset', String(opts.offset))
  return libFetch<LibrarySearch>(`/articles/search?${params.toString()}`)
}

/** Company-profile gaps with histogram + top tier-3 titles. */
export async function fetchCharacters(): Promise<LibraryCharacters> {
  return libFetch<LibraryCharacters>('/characters')
}

// ---------------------------------------------------------------------------
// v3 — Manuscript reader
// ---------------------------------------------------------------------------

/** Fetch the full parsed manuscript structure (cached server-side). */
export async function fetchManuscriptStructure(): Promise<ManuscriptStructure> {
  return libFetch<ManuscriptStructure>('/manuscript/structure')
}

/** Force-refresh the manuscript parser cache and return parse stats. */
export async function refreshManuscript(): Promise<{
  paragraph_count: number
  gap_link_count: number
  last_modified: string
}> {
  return libPost('/manuscript/refresh', {})
}

// ---------------------------------------------------------------------------
// v3 — User marks (DB-backed)
// ---------------------------------------------------------------------------

async function libPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`API POST ${path} → ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

/** Upsert a mark. Only provided fields are updated. */
export async function upsertMark(payload: {
  article_id: number
  starred?: boolean
  read?: boolean
  note?: string
}): Promise<MarkRow> {
  return libPost<MarkRow>('/marks', payload)
}

/** Bulk fetch marks (optionally filtered). */
export async function fetchMarks(filters: {
  starred?: boolean
  read?: boolean
} = {}): Promise<{ marks: MarkRow[] }> {
  const params = new URLSearchParams()
  if (filters.starred !== undefined) params.set('starred', filters.starred ? 'true' : 'false')
  if (filters.read !== undefined) params.set('read', filters.read ? 'true' : 'false')
  const qs = params.toString()
  return libFetch<{ marks: MarkRow[] }>(`/marks${qs ? '?' + qs : ''}`)
}

/** Resolve article_ids → gap_ids they belong to. */
export async function resolveGaps(
  articleIds: number[],
): Promise<{ mapping: Record<string, string[]> }> {
  return libPost<{ mapping: Record<string, string[]> }>('/articles/resolve_gaps', {
    article_ids: articleIds,
  })
}
