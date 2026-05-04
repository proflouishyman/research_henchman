// Typed fetch wrappers for the writing-companion library API.
// Mirrors the conventions of ``lib/api.ts``.

import type { LibraryDossier, LibraryIndex, GapTreeRow } from '../types/library'

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
