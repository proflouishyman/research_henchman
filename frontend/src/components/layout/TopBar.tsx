// Top navigation bar: logo, mode toggle (Runs/Write), settings, dark mode toggle.

import { BookOpen, Settings, Sun, Moon, Plus } from 'lucide-react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useUIStore } from '../../store/ui'

interface TopBarProps {
  /** When true, hide the New Run button (e.g. on the Write tab). */
  hideNewRun?: boolean
}

export function TopBar({ hideNewRun = false }: TopBarProps) {
  const { darkMode, toggleDarkMode, setNewRunModalOpen, setSettingsModalOpen } = useUIStore()
  const location = useLocation()
  const navigate = useNavigate()

  const mode: 'runs' | 'write' = location.pathname.startsWith('/write') ? 'write' : 'runs'

  return (
    <header className="h-14 flex items-center justify-between px-5 border-b border-border bg-surface-card shrink-0 z-20">
      {/* Logo + mode toggle */}
      <div className="flex items-center gap-4">
        <Link to="/runs" className="flex items-center gap-2.5">
          <div className="w-7 h-7 bg-accent rounded-md flex items-center justify-center">
            <BookOpen size={14} className="text-white" strokeWidth={2.5} />
          </div>
          <span className="font-semibold text-ink text-sm tracking-tight">Research Henchman</span>
        </Link>

        {/* Mode toggle — flips top-level route tree. */}
        <div className="flex items-center bg-surface-muted rounded-md p-0.5 border border-border">
          <button
            onClick={() => navigate('/runs')}
            className={`text-xs font-medium px-3 py-1 rounded transition-colors ${
              mode === 'runs'
                ? 'bg-surface-card text-ink shadow-sm'
                : 'text-ink-secondary hover:text-ink'
            }`}
          >
            Runs
          </button>
          <button
            onClick={() => navigate('/write')}
            className={`text-xs font-medium px-3 py-1 rounded transition-colors ${
              mode === 'write'
                ? 'bg-surface-card text-ink shadow-sm'
                : 'text-ink-secondary hover:text-ink'
            }`}
          >
            Write
          </button>
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        {!hideNewRun && (
          <button
            onClick={() => setNewRunModalOpen(true)}
            className="flex items-center gap-1.5 text-xs font-medium bg-accent text-white px-3 py-1.5 rounded-md hover:bg-accent-hover transition-colors"
          >
            <Plus size={13} strokeWidth={2.5} />
            New Run
          </button>
        )}

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
