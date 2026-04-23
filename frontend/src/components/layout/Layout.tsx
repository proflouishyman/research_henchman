// Three-column app layout: sidebar | main pipeline | evidence panel.

import { TopBar } from './TopBar'
import { RunSidebar } from '../runs/RunSidebar'
import { PipelineView } from '../pipeline/PipelineView'
import { EvidencePanel } from '../evidence/EvidencePanel'
import { ManuscriptPicker } from '../runs/ManuscriptPicker'
import { SettingsModal } from '../settings/SettingsModal'
import { useUIStore } from '../../store/ui'

export function Layout() {
  const { evidencePanelOpen, newRunModalOpen, settingsModalOpen } = useUIStore()

  return (
    <div className="flex flex-col h-screen bg-surface">
      <TopBar />

      <div className="flex flex-1 overflow-hidden">
        {/* Left sidebar: run list */}
        <aside className="w-64 shrink-0 border-r border-border overflow-y-auto bg-surface-card">
          <RunSidebar />
        </aside>

        {/* Main pipeline content */}
        <main className="flex-1 overflow-y-auto">
          <PipelineView />
        </main>

        {/* Right evidence panel (slides in/out) */}
        <EvidencePanel open={evidencePanelOpen} />
      </div>

      {/* Modals */}
      {newRunModalOpen && <ManuscriptPicker />}
      {settingsModalOpen && <SettingsModal />}
    </div>
  )
}
