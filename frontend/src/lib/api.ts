// Typed fetch wrappers for all backend API endpoints.
// All requests go to the FastAPI backend at /api/orchestrator/...

import type {
  RunRow,
  Event,
  GapPacket,
  SignInTarget,
  SignInResult,
  Source,
  Settings,
  Manuscript,
  LinkedDocument,
} from '../types/contracts'

const BASE = '/api/orchestrator'

// Generic JSON fetch helper with error surfacing
async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`API ${path} → ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// --- Runs ---

export async function fetchRuns(): Promise<RunRow[]> {
  const data = await apiFetch<{ runs: RunRow[] }>('/runs')
  return data.runs
}

export async function fetchRun(runId: string): Promise<RunRow> {
  return apiFetch<RunRow>(`/runs/${runId}`)
}

export async function createRun(manuscriptPath: string): Promise<{ run_id: string }> {
  return apiFetch<{ run_id: string }>('/runs', {
    method: 'POST',
    body: JSON.stringify({ manuscript_path: manuscriptPath }),
  })
}

export async function retryRun(runId: string): Promise<{ run_id: string }> {
  return apiFetch<{ run_id: string }>(`/runs/${runId}/retry`, { method: 'POST' })
}

// --- Events ---

export async function fetchEvents(runId: string): Promise<Event[]> {
  const data = await apiFetch<{ run_id: string; events: Event[] }>(`/runs/${runId}/events`)
  return data.events
}

// --- Documents / Evidence ---

export async function fetchDocuments(runId: string): Promise<GapPacket[]> {
  const data = await apiFetch<{ run_id: string; gap_packets: GapPacket[] }>(
    `/runs/${runId}/documents`
  )
  return data.gap_packets
}

export async function fetchEvidence(evidenceId: string): Promise<LinkedDocument> {
  return apiFetch<LinkedDocument>(`/evidence/${evidenceId}`)
}

// --- Manuscripts ---

export async function fetchManuscripts(): Promise<Manuscript[]> {
  const data = await apiFetch<{ manuscripts: Manuscript[] }>('/manuscripts')
  return data.manuscripts
}

// --- Sources ---

export async function fetchSources(): Promise<Source[]> {
  const data = await apiFetch<{ sources: Source[] }>('/sources/catalog')
  return data.sources ?? []
}

// --- Settings ---

export async function fetchSettings(): Promise<Settings> {
  return apiFetch<Settings>('/connections/values')
}

export async function saveSettings(updates: Record<string, string>): Promise<void> {
  await apiFetch('/connections/save', {
    method: 'POST',
    body: JSON.stringify({ updates }),
  })
}

// --- Sign-in ---

export async function fetchSignInPreflight(manuscriptPath: string): Promise<SignInTarget[]> {
  const data = await apiFetch<{ targets: SignInTarget[] }>('/signin/preflight', {
    method: 'POST',
    body: JSON.stringify({ manuscript_path: manuscriptPath }),
  })
  return data.targets ?? []
}

export async function testSignIn(
  sourceIds: string[],
  manuscriptPath?: string
): Promise<SignInResult[]> {
  const data = await apiFetch<{ results: SignInResult[] }>('/signin/test', {
    method: 'POST',
    body: JSON.stringify({ source_ids: sourceIds, manuscript_path: manuscriptPath ?? '' }),
  })
  return data.results ?? []
}

export async function openSignIn(sourceIds: string[], urls: string[]): Promise<{ opened: number }> {
  return apiFetch<{ opened: number }>('/signin/open', {
    method: 'POST',
    body: JSON.stringify({ source_ids: sourceIds, urls }),
  })
}

// --- Library profiles ---

export async function fetchLibraryProfiles(): Promise<string[]> {
  const data = await apiFetch<{ profiles: string[] }>('/library/profiles')
  return data.profiles ?? []
}
