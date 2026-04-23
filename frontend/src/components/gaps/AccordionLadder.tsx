// Collapsible accordion ladder showing query rungs and synonym ring.

import type { AccordionLadder as AccordionLadderType } from '../../types/contracts'

interface AccordionLadderProps {
  ladder: AccordionLadderType
}

function QueryRung({ label, query }: { label: string; query: string }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="text-ink-muted font-medium w-20 shrink-0 text-right">{label}</span>
      <span className="font-mono text-ink-secondary leading-relaxed">{query}</span>
    </div>
  )
}

export function AccordionLadder({ ladder }: AccordionLadderProps) {
  const ring = ladder.synonym_ring

  return (
    <details className="group">
      <summary className="cursor-pointer flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink-secondary transition-colors list-none select-none py-1">
        <span className="group-open:rotate-90 transition-transform inline-block">▶</span>
        <span className="font-medium">Query Ladder</span>
        {ladder.primary_term && (
          <span className="text-ink-muted font-mono">— {ladder.primary_term}</span>
        )}
        <span className="ml-auto text-[10px] bg-surface-muted border border-border px-1.5 py-0.5 rounded">
          {ladder.generation_method}
        </span>
      </summary>

      <div className="mt-2 space-y-1.5 pl-3 border-l border-border/70">
        <QueryRung label="Constrained" query={ladder.constrained} />
        <QueryRung label="Contextual" query={ladder.contextual} />
        <QueryRung label="Broad" query={ladder.broad} />
        <QueryRung label="Fallback" query={ladder.fallback} />
      </div>

      {/* Synonym ring */}
      {ring && (
        <div className="mt-3 pl-3 space-y-2">
          {ring.era_start !== undefined && ring.era_end !== undefined && (
            <div className="flex items-center gap-2 text-[10px]">
              <span className="text-ink-muted font-medium">Era range</span>
              <span className="font-mono text-accent">
                {ring.era_start}–{ring.era_end}
              </span>
            </div>
          )}

          {ring.terminology_shifts.length > 0 && (
            <div>
              <p className="text-[10px] text-ink-muted mb-1 font-medium">Terminology shifts</p>
              <div className="flex flex-wrap gap-1">
                {ring.terminology_shifts.map((t) => (
                  <span
                    key={t}
                    className="text-[10px] font-mono bg-surface-muted border border-border px-1.5 py-0.5 rounded text-ink-secondary"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </div>
          )}

          {ring.institutional_names.length > 0 && (
            <div>
              <p className="text-[10px] text-ink-muted mb-1 font-medium">Institutional names</p>
              <div className="flex flex-wrap gap-1">
                {ring.institutional_names.map((n) => (
                  <span
                    key={n}
                    className="text-[10px] font-mono bg-blue-50 border border-blue-200 px-1.5 py-0.5 rounded text-blue-700"
                  >
                    {n}
                  </span>
                ))}
              </div>
            </div>
          )}

          {ring.era_modifiers.length > 0 && (
            <div>
              <p className="text-[10px] text-ink-muted mb-1 font-medium">Era modifiers</p>
              <div className="flex flex-wrap gap-1">
                {ring.era_modifiers.map((m) => (
                  <span
                    key={m}
                    className="text-[10px] font-mono bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded text-amber-700"
                  >
                    {m}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </details>
  )
}
