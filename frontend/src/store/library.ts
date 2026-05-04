// Zustand store for writing-companion / library mode UI state.
//
// Holds per-dossier filter state (source toggles, score floor, has-PDF
// only) and the set of expanded tier sections. Persists nothing
// (defaults are deterministic across reloads — keeps things simple).

import { create } from 'zustand'

export interface DossierFilters {
  /** When non-empty, only show entries whose source_id is in the set. */
  sourceIds: string[]
  /** Minimum relevance score to display (3, 2, 1, or 0). */
  scoreMin: number
  /** When true, hide entries that lack a local pdf_path. */
  hasPdf: boolean
}

interface LibraryState {
  selectedGapId: string | null
  setSelectedGapId: (id: string | null) => void

  dossierFilters: DossierFilters
  setSourceIds: (ids: string[]) => void
  toggleSourceId: (id: string) => void
  setScoreMin: (n: number) => void
  setHasPdf: (b: boolean) => void
  resetFilters: () => void

  /** Tier buckets currently expanded (default {3,2}). */
  expandedTiers: string[]
  toggleTier: (bucket: string) => void
}

const DEFAULT_FILTERS: DossierFilters = {
  sourceIds: [],
  scoreMin: 0,
  hasPdf: false,
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
}))
