// One stage pill in the pipeline rail.

import { motion } from 'framer-motion'
import {
  Search,
  Brain,
  Download,
  Database,
  Star,
  CheckCircle,
  type LucideIcon,
} from 'lucide-react'

export type StageState = 'pending' | 'active' | 'complete' | 'failed'

export interface Stage {
  id: string
  label: string
  icon: LucideIcon
}

export const STAGES: Stage[] = [
  { id: 'analyzing', label: 'Analyzing', icon: Search },
  { id: 'planning', label: 'Planning', icon: Brain },
  { id: 'pulling', label: 'Pulling', icon: Download },
  { id: 'ingesting', label: 'Ingesting', icon: Database },
  { id: 'fitting', label: 'Fitting', icon: Star },
  { id: 'complete', label: 'Complete', icon: CheckCircle },
]

interface StageIndicatorProps {
  stage: Stage
  state: StageState
  isLast?: boolean
}

export function StageIndicator({ stage, state, isLast }: StageIndicatorProps) {
  const Icon = stage.icon

  const pillClasses = {
    pending: 'bg-surface-muted border-border text-ink-muted',
    active: 'bg-emerald-50 border-emerald-300 text-emerald-700',
    complete: 'bg-gray-100 border-gray-300 text-gray-600',
    failed: 'bg-red-50 border-red-300 text-red-600',
  }[state]

  const dotClasses = {
    pending: 'bg-gray-300',
    active: 'bg-emerald-500',
    complete: 'bg-gray-500',
    failed: 'bg-red-500',
  }[state]

  return (
    <div className="flex items-center">
      <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-xs font-medium ${pillClasses} transition-all duration-300`}>
        {/* Status dot */}
        <span className="relative flex items-center justify-center w-2 h-2">
          {state === 'active' && (
            <motion.span
              className="absolute inline-flex w-full h-full rounded-full bg-emerald-400 opacity-75"
              animate={{ scale: [1, 1.8, 1], opacity: [0.75, 0, 0.75] }}
              transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
            />
          )}
          <span className={`relative inline-flex rounded-full w-1.5 h-1.5 ${dotClasses}`} />
        </span>

        <Icon size={11} strokeWidth={2} />
        <span>{stage.label}</span>
      </div>

      {/* Connector line */}
      {!isLast && (
        <div className={`w-4 h-px mx-1 ${state === 'complete' ? 'bg-gray-400' : 'bg-border'}`} />
      )}
    </div>
  )
}
