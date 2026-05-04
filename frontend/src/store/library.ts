// Zustand store for writing-companion / library mode UI state.
//
// Holds:
//   * per-dossier filter state (source toggles, score floor, has-PDF)
//   * tier section expanded/collapsed bookkeeping
//   * search query + filters + result accumulator (Load More semantics)
//   * marks (star + read) keyed by article id — v3: DB-backed via API
//   * manuscript structure + reader UI state (v3)
//
// v3 migration: marks now live in article_index.sqlite via the marks API.
// On first load, any marks in localStorage['library.marks'] are migrated
// to the DB then cleared. The store reads/writes via the API.

import { create } from 'zustand'
import type { ManuscriptStructure, SearchFilters, SearchResult } from '../types/library'
import { upsertMark, fetchMarks } from '../lib/library_api'

export interface DossierFilters {
  /** When non-empty, only show entries whose source_id is in the set. */
  sourceIds: string[]
  /** Minimum relevance score to display (3, 2, 1, or 0). */
  scoreMin: number
  /** When true, hide entries that lack a local pdf_path. */
  hasPdf: boolean
}

export interface ArticleMark {
  starred: boolean
  read: boolean
  /** Epoch milliseconds when the mark was first set. Used to sort the queue. */
  addedAt: number
}

const DEFAULT_FILTERS: DossierFilters = {
  sourceIds: [],
  scoreMin: 0,
  hasPdf: false,
}

const DEFAULT_SEARCH_FILTERS: SearchFilters = {
  sourceIds: [],
  scoreMin: 0,
  gapId: '',
  yearFrom: null,
  yearTo: null,
  hasPdf: null,
}

const MARKS_KEY = 'library.marks'

/** Hydrate marks dict from localStorage (legacy Wave 2 format). */
function _loadLegacyMarks(): Record<number, ArticleMark> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(MARKS_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, ArticleMark>
    const out: Record<number, ArticleMark> = {}
    for (const [k, v] of Object.entries(parsed)) {
      const id = Number(k)
      if (Number.isFinite(id) && v && typeof v === 'object') {
        out[id] = {
          starred: !!v.starred,
          read: !!v.read,
          addedAt: typeof v.addedAt === 'number' ? v.addedAt : Date.now(),
        }
      }
    }
    return out
  } catch {
    return {}
  }
}

/**
 * Migrate any Wave 2 localStorage marks to the DB, then clear localStorage.
 * Idempotent: if localStorage is already empty this is a no-op.
 */
async function _migrateLegacyMarks(
  setMarks: (m: Record<number, ArticleMark>) => void,
): Promise<void> {
  const legacy = _loadLegacyMarks()
  const entries = Object.entries(legacy)
  if (entries.length === 0) return

  // Upload each mark to the API.
  for (const [idStr, mark] of entries) {
    const id = Number(idStr)
    if (!Number.isFinite(id)) continue
    try {
      await upsertMark({ article_id: id, starred: mark.starred, read: mark.read })
    } catch {
      // Best-effort — don't block migration on a single failure.
    }
  }

  // Clear localStorage once migration is done.
  try {
    window.localStorage.removeItem(MARKS_KEY)
  } catch {
    /* private mode */
  }

  // Reload from DB so in-memory state is fresh.
  try {
    const { marks: dbMarks } = await fetchMarks()
    const next: Record<number, ArticleMark> = {}
    for (const m of dbMarks) {
      next[m.article_id] = { starred: m.starred, read: m.read, addedAt: Date.now() }
    }
    setMarks(next)
  } catch {
    // Fallback: keep the legacy in-memory marks.
    setMarks(legacy)
  }
}

interface LibraryState {
  selectedGapId: string | null
  setSelectedGapId: (id: string | null) => void

  // Dossier filter state
  dossierFilters: DossierFilters
  setSourceIds: (ids: string[]) => void
  toggleSourceId: (id: string) => void
  setScoreMin: (n: number) => void
  setHasPdf: (b: boolean) => void
  resetFilters: () => void

  /** Tier buckets currently expanded (default {3,2}). */
  expandedTiers: string[]
  toggleTier: (bucket: string) => void

  // ---- Wave 2: search ----
  searchQuery: string
  searchFilters: SearchFilters
  searchResults: SearchResult[]
  searchTotal: number
  searchOffset: number
  searchLoading: boolean
  searchError: string | null
  setSearchQuery: (q: string) => void
  setSearchFilters: (f: Partial<SearchFilters>) => void
  resetSearchFilters: () => void
  setSearchResults: (results: SearchResult[], total: number, offset: number) => void
  appendSearchResults: (results: SearchResult[], total: number, offset: number) => void
  setSearchLoading: (b: boolean) => void
  setSearchError: (msg: string | null) => void
  clearSearch: () => void

  // ---- Wave 2 / v3: marks (star + read) — now DB-backed ----
  marks: Record<number, ArticleMark>
  _setMarks: (m: Record<number, ArticleMark>) => void
  toggleStar: (articleId: number) => void
  toggleRead: (articleId: number) => void
  isStarred: (articleId: number) => boolean
  isRead: (articleId: number) => boolean
  starredCount: () => number
  starredIds: () => number[]
  /** Call once on app mount to hydrate from DB + migrate legacy localStorage. */
  hydrateMarks: () => Promise<void>

  // ---- v3: manuscript reader ----
  manuscriptStructure: ManuscriptStructure | null
  manuscriptLoading: boolean
  manuscriptError: string | null
  selectedChapter: string | null
  selectedParaId: string | null
  dossierSidePanelOpen: boolean
  setManuscriptStructure: (s: ManuscriptStructure | null) => void
  setManuscriptLoading: (b: boolean) => void
  setManuscriptError: (msg: string | null) => void
  setSelectedChapter: (ch: string | null) => void
  setSelectedParaId: (id: string | null) => void
  setDossierSidePanelOpen: (open: boolean) => void
}

export const useLibraryStore = create<LibraryState>((set, get) => ({
  selectedGapId: null,
  setSelectedGapId: (id) => set({ selectedGapId: id }),

  dossierFilters: DEFAULT_FILTERS,
  setSourceIds: (ids) =>
    set((s) => ({ dossierFilters: { ...s.dossierFilters, sourceIds: ids } })),
  toggleSourceId: (id) => {
    const cur = get().dossierFilters.sourceIds
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    set((s) => ({ dossierFilters: { ...s.dossierFilters, sourceIds: next } }))
  },
  setScoreMin: (n) =>
    set((s) => ({ dossierFilters: { ...s.dossierFilters, scoreMin: n } })),
  setHasPdf: (b) =>
    set((s) => ({ dossierFilters: { ...s.dossierFilters, hasPdf: b } })),
  resetFilters: () => set({ dossierFilters: DEFAULT_FILTERS }),

  expandedTiers: ['3', '2'],
  toggleTier: (bucket) => {
    const cur = get().expandedTiers
    const next = cur.includes(bucket) ? cur.filter((x) => x !== bucket) : [...cur, bucket]
    set({ expandedTiers: next })
  },

  // ---- Search ----
  searchQuery: '',
  searchFilters: DEFAULT_SEARCH_FILTERS,
  searchResults: [],
  searchTotal: 0,
  searchOffset: 0,
  searchLoading: false,
  searchError: null,
  setSearchQuery: (q) => set({ searchQuery: q }),
  setSearchFilters: (f) =>
    set((s) => ({ searchFilters: { ...s.searchFilters, ...f } })),
  resetSearchFilters: () => set({ searchFilters: DEFAULT_SEARCH_FILTERS }),
  setSearchResults: (results, total, offset) =>
    set({ searchResults: results, searchTotal: total, searchOffset: offset }),
  appendSearchResults: (results, total, offset) =>
    set((s) => ({
      searchResults: [...s.searchResults, ...results],
      searchTotal: total,
      searchOffset: offset,
    })),
  setSearchLoading: (b) => set({ searchLoading: b }),
  setSearchError: (msg) => set({ searchError: msg }),
  clearSearch: () =>
    set({
      searchResults: [],
      searchTotal: 0,
      searchOffset: 0,
      searchError: null,
    }),

  // ---- v3 Marks (DB-backed) ----
  // Initialize from localStorage (legacy) so the UI isn't empty on first
  // mount before hydrateMarks() resolves. hydrateMarks() will overwrite.
  marks: _loadLegacyMarks(),
  _setMarks: (m) => set({ marks: m }),

  toggleStar: (articleId) => {
    const cur = get().marks
    const existing = cur[articleId]
    const newStarred = !(existing?.starred ?? false)
    const next: Record<number, ArticleMark> = {
      ...cur,
      [articleId]: {
        starred: newStarred,
        read: existing?.read ?? false,
        addedAt: existing?.addedAt ?? Date.now(),
      },
    }
    if (!newStarred && !next[articleId].read) {
      delete next[articleId]
    }
    set({ marks: next })
    // Persist to DB (fire-and-forget; UI is already optimistically updated).
    upsertMark({ article_id: articleId, starred: newStarred }).catch(() => undefined)
  },

  toggleRead: (articleId) => {
    const cur = get().marks
    const existing = cur[articleId]
    const newRead = !(existing?.read ?? false)
    const next: Record<number, ArticleMark> = {
      ...cur,
      [articleId]: {
        starred: existing?.starred ?? false,
        read: newRead,
        addedAt: existing?.addedAt ?? Date.now(),
      },
    }
    if (!newRead && !next[articleId].starred) {
      delete next[articleId]
    }
    set({ marks: next })
    upsertMark({ article_id: articleId, read: newRead }).catch(() => undefined)
  },

  isStarred: (articleId) => !!get().marks[articleId]?.starred,
  isRead: (articleId) => !!get().marks[articleId]?.read,
  starredCount: () => Object.values(get().marks).filter((m) => m.starred).length,
  starredIds: () =>
    Object.entries(get().marks)
      .filter(([, m]) => m.starred)
      .sort((a, b) => (b[1].addedAt || 0) - (a[1].addedAt || 0))
      .map(([id]) => Number(id)),

  hydrateMarks: async () => {
    const setMarks = (m: Record<number, ArticleMark>) => get()._setMarks(m)
    // Migrate legacy localStorage marks (idempotent if already clear).
    await _migrateLegacyMarks(setMarks)
    // Reload from DB to get the canonical state.
    try {
      const { marks: dbMarks } = await fetchMarks()
      const next: Record<number, ArticleMark> = {}
      for (const m of dbMarks) {
        next[m.article_id] = { starred: m.starred, read: m.read, addedAt: Date.now() }
      }
      set({ marks: next })
    } catch {
      // Non-fatal — keep whatever state we have.
    }
  },

  // ---- v3 Manuscript reader ----
  manuscriptStructure: null,
  manuscriptLoading: false,
  manuscriptError: null,
  selectedChapter: null,
  selectedParaId: null,
  dossierSidePanelOpen: false,
  setManuscriptStructure: (s) => set({ manuscriptStructure: s }),
  setManuscriptLoading: (b) => set({ manuscriptLoading: b }),
  setManuscriptError: (msg) => set({ manuscriptError: msg }),
  setSelectedChapter: (ch) => set({ selectedChapter: ch }),
  setSelectedParaId: (id) => set({ selectedParaId: id }),
  setDossierSidePanelOpen: (open) => set({ dossierSidePanelOpen: open }),
}))
