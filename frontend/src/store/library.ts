// Zustand store for writing-companion / library mode UI state.
//
// Holds:
//   * per-dossier filter state (source toggles, score floor, has-PDF)
//   * tier section expanded/collapsed bookkeeping
//   * search query + filters + result accumulator (Load More semantics)
//   * marks (star + read) keyed by article id, persisted to localStorage
//
// Marks are intentionally browser-local in Wave 2 — defer the server-side
// table to v3. Hydration runs once on store creation; persistence runs on
// every mutation through ``_persistMarks``.

import { create } from 'zustand'
import type { SearchFilters, SearchResult } from '../types/library'

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

/** Hydrate marks dict from localStorage. Returns {} on any failure. */
function _loadMarks(): Record<number, ArticleMark> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(MARKS_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, ArticleMark>
    // Normalize keys (JSON serialises numbers as strings).
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

/** Persist marks dict to localStorage; silent on failure (private mode etc.). */
function _persistMarks(marks: Record<number, ArticleMark>): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(MARKS_KEY, JSON.stringify(marks))
  } catch {
    /* quota exceeded / private mode — silently no-op. */
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

  // ---- Wave 2: marks (star + read) ----
  marks: Record<number, ArticleMark>
  toggleStar: (articleId: number) => void
  toggleRead: (articleId: number) => void
  isStarred: (articleId: number) => boolean
  isRead: (articleId: number) => boolean
  starredCount: () => number
  starredIds: () => number[]
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

  // ---- Marks ----
  marks: _loadMarks(),
  toggleStar: (articleId) => {
    const cur = get().marks
    const existing = cur[articleId]
    const next: Record<number, ArticleMark> = { ...cur }
    if (existing && existing.starred) {
      // Un-starring: keep the entry only if read is also true.
      if (existing.read) {
        next[articleId] = { ...existing, starred: false }
      } else {
        delete next[articleId]
      }
    } else {
      next[articleId] = {
        starred: true,
        read: existing?.read ?? false,
        addedAt: existing?.addedAt ?? Date.now(),
      }
    }
    _persistMarks(next)
    set({ marks: next })
  },
  toggleRead: (articleId) => {
    const cur = get().marks
    const existing = cur[articleId]
    const next: Record<number, ArticleMark> = { ...cur }
    if (existing && existing.read) {
      // Un-marking read: keep the entry only if still starred.
      if (existing.starred) {
        next[articleId] = { ...existing, read: false }
      } else {
        delete next[articleId]
      }
    } else {
      next[articleId] = {
        starred: existing?.starred ?? false,
        read: true,
        addedAt: existing?.addedAt ?? Date.now(),
      }
    }
    _persistMarks(next)
    set({ marks: next })
  },
  isStarred: (articleId) => !!get().marks[articleId]?.starred,
  isRead: (articleId) => !!get().marks[articleId]?.read,
  starredCount: () =>
    Object.values(get().marks).filter((m) => m.starred).length,
  starredIds: () =>
    Object.entries(get().marks)
      .filter(([, m]) => m.starred)
      .sort((a, b) => (b[1].addedAt || 0) - (a[1].addedAt || 0))
      .map(([id]) => Number(id)),
}))
