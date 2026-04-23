// Top navigation bar: logo, settings button, dark mode toggle.

import { BookOpen, Settings, Sun, Moon, Plus } from 'lucide-react'
import { useUIStore } from '../../store/ui'

export function TopBar() {
  const { darkMode, toggleDarkMode, setNewRunModalOpen, setSettingsModalOpen } = useUIStore()

  return (
    <header className="h-14 flex items-center justify-between px-5 border-b border-border bg-surface-card shrink-0 z-20">
      {/* Logo */}
      <div className="flex items-center gap-2.5">
        <div className="w-7 h-7 bg-accent rounded-md flex items-center justify-center">
          <BookOpen size={14} className="text-white" strokeWidth={2.5} />
        </div>
        <span className="font-semibold text-ink text-sm tracking-tight">Research Henchman</span>
        <span className="text-ink-muted text-xs ml-1 hidden sm:block">manuscript gap analysis</span>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => setNewRunModalOpen(true)}
          className="flex items-center gap-1.5 text-xs font-medium bg-accent text-white px-3 py-1.5 rounded-md hover:bg-accent-hover transition-colors"
        >
          <Plus size={13} strokeWidth={2.5} />
          New Run
        </button>

        <button
          onClick={() => setSettingsModalOpen(true)}
          className="p-2 rounded-md text-ink-secondary hover:text-ink hover:bg-surface-muted transition-colors"
          title="Settings"
        >
          <Settings size={16} />
        </button>

        <button
          onClick={toggleDarkMode}
          className="p-2 rounded-md text-ink-secondary hover:text-ink hover:bg-surface-muted transition-colors"
          title={darkMode ? 'Light mode' : 'Dark mode'}
        >
          {darkMode ? <Sun size={16} /> : <Moon size={16} />}
        </button>
      </div>
    </header>
  )
}
