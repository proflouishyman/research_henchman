// Zustand UI store: cross-component UI state that doesn't need server sync.

import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface UIState {
  /** Currently selected run ID */
  selectedRunId: string | null
  setSelectedRunId: (id: string | null) => void

  /** Currently selected gap ID (opens evidence panel) */
  selectedGapId: string | null
  setSelectedGapId: (id: string | null) => void

  /** Whether the evidence side panel is open */
  evidencePanelOpen: boolean
  setEvidencePanelOpen: (open: boolean) => void

  /** Dark mode toggle */
  darkMode: boolean
  toggleDarkMode: () => void

  /** New run modal open */
  newRunModalOpen: boolean
  setNewRunModalOpen: (open: boolean) => void

  /** Settings modal open */
  settingsModalOpen: boolean
  setSettingsModalOpen: (open: boolean) => void
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      selectedRunId: null,
      setSelectedRunId: (id) =>
        set({ selectedRunId: id, selectedGapId: null, evidencePanelOpen: false }),

      selectedGapId: null,
      setSelectedGapId: (id) => set({ selectedGapId: id, evidencePanelOpen: id !== null }),

      evidencePanelOpen: false,
      setEvidencePanelOpen: (open) => set({ evidencePanelOpen: open }),

      darkMode: false,
      toggleDarkMode: () => set((s) => ({ darkMode: !s.darkMode })),

      newRunModalOpen: false,
      setNewRunModalOpen: (open) => set({ newRunModalOpen: open }),

      settingsModalOpen: false,
      setSettingsModalOpen: (open) => set({ settingsModalOpen: open }),
    }),
    {
      name: 'research-henchman-ui',
      // Only persist dark mode preference; runtime state resets on load
      partialize: (state) => ({ darkMode: state.darkMode }),
    }
  )
)
