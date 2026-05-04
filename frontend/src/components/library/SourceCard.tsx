// Source card — the heart of the writing-companion preview UX.
//
// User non-negotiables (verbatim):
//   1. "open-source must be one click" — PDF on disk → /api/orchestrator/files
//      opens the file; URL → window.open in a new tab.
//   2. "preview before clicking" — relevance_why is the prominent callout,
//      abstract sits below truncated to ~3 lines; hover surfaces the full
//      WHY + abstract in a side popover at 300ms.
//
// The card is also draggable: dataTransfer carries Chicago / short / link
// forms so the user can drag straight into Word/Pages.
//
// Wave 2 polish:
//   * Hover action toolbar at top-right: Copy (3 forms) + Star + Read.
//   * Optional ``snippet`` prop renders an FTS-highlighted excerpt (with
//     <mark> tags) below the abstract preview. Only the <mark> tag is
//     allowed — every other tag is escaped via DOMParser/textContent.
//   * Drag handle icon (grip) appears on hover to advertise draggability.
//   * Compact star icon visible all the time when starred.

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ExternalLink,
  Copy,
  Check,
  FileText,
  Link as LinkIcon,
  Network,
  Star,
  Eye,
  GripVertical,
} from 'lucide-react'
import type { DossierEntry } from '../../types/library'
import { fileUrl } from '../../lib/library_api'
import { attachDragCitations, chicagoCitation, shortCitation, linkOnly } from '../../lib/citations'
import { useLibraryStore } from '../../store/library'
import { showToast } from './Toast'

interface Props {
  entry: DossierEntry
  /** Compact single-line rendering for tier 0/1. */
  compact?: boolean
  /** Optional FTS snippet to render under the abstract (HTML with <mark>). */
  snippet?: string
}

const SOURCE_LABELS: Record<string, string> = {
  ebsco_api: 'EBSCO',
  proquest_us_newsstream: 'ProQuest US',
  proquest_international_newsstream: 'ProQuest Intl',
  proquest_historical_newspapers: 'ProQuest Historical',
  hathitrust_fulltext: 'HathiTrust',
  sec_edgar: 'SEC EDGAR',
}

function srcLabel(id: string): string {
  return SOURCE_LABELS[id] || id
}

function tierBadgeClass(score: number | null): string {
  if (score === 3) return 'bg-emerald-50 text-emerald-700 border-emerald-200'
  if (score === 2) return 'bg-blue-50 text-blue-700 border-blue-200'
  if (score === 1) return 'bg-gray-100 text-gray-600 border-gray-200'
  if (score === 0) return 'bg-gray-50 text-gray-400 border-gray-200'
  return 'bg-gray-100 text-gray-500 border-gray-200'
}

/**
 * Sanitize an FTS snippet for innerHTML rendering.
 *
 * Strategy: escape every HTML special char, then re-introduce
 * ``<mark>...</mark>`` only — FTS5 emits these tags from a
 * developer-controlled template, so we know they're the only ones we
 * need. This is robust against any user input that might contain
 * accidental HTML, while preserving the highlight UX.
 */
function renderSnippetHtml(snippet: string): string {
  // Use sentinels that can't appear in escaped HTML so we can re-inject
  // the marks safely after escaping.
  const OPEN = 'MARKO'
  const CLOSE = 'MARKC'
  const withSentinels = snippet
    .replace(/<mark>/g, OPEN)
    .replace(/<\/mark>/g, CLOSE)
  const escaped = withSentinels
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
  return escaped
    .replace(new RegExp(OPEN, 'g'), '<mark class="bg-accent-light text-accent rounded px-0.5">')
    .replace(new RegExp(CLOSE, 'g'), '</mark>')
}

export function SourceCard({ entry, compact, snippet }: Props) {
  const [showAbstract, setShowAbstract] = useState(false)
  const [hoverOpen, setHoverOpen] = useState(false)
  const hoverTimer = useRef<number | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)

  const onMouseEnter = () => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => setHoverOpen(true), 300)
  }
  const onMouseLeave = () => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    setHoverOpen(false)
  }
  useEffect(() => {
    return () => {
      if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    }
  }, [])

  const onOpen = () => {
    if (entry.pdf_path) {
      window.open(fileUrl(entry.pdf_path), '_blank')
    } else if (entry.url) {
      window.open(entry.url, '_blank')
    }
  }

  const handleDragStart = (ev: React.DragEvent) => {
    attachDragCitations(ev, entry)
  }

  const meta: string[] = []
  if (entry.authors) meta.push(entry.authors)
  if (entry.pub_date) meta.push(entry.pub_date)
  meta.push(srcLabel(entry.source_id))
  if (entry.also_in_sources.length > 0) {
    meta.push(`also in: ${entry.also_in_sources.map(srcLabel).join(', ')}`)
  }

  // Compact single-line variant (tier 0/1).
  if (compact) {
    return (
      <div
        ref={cardRef}
        data-testid="source-card"
        draggable
        onDragStart={handleDragStart}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        className="relative flex items-start gap-2 px-2 py-1.5 rounded hover:bg-surface-muted transition-colors group"
      >
        <DragHandle />
        <span
          className={`text-[10px] font-mono px-1.5 py-0.5 rounded border shrink-0 mt-0.5 ${tierBadgeClass(
            entry.relevance_score,
          )}`}
        >
          {entry.relevance_score ?? '—'}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-xs text-ink truncate">
            <span className="font-medium">{entry.title || '(untitled)'}</span>
            <span className="text-ink-muted"> — {meta.join(' · ')}</span>
          </div>
          {entry.relevance_why && (
            <div className="text-[11px] italic text-ink-secondary line-clamp-1 mt-0.5">
              {entry.relevance_why}
            </div>
          )}
          {snippet && (
            <div
              className="text-[11px] text-ink-secondary line-clamp-1 mt-0.5"
              // eslint-disable-next-line react/no-danger
              dangerouslySetInnerHTML={{ __html: renderSnippetHtml(snippet) }}
            />
          )}
        </div>
        <CardActions entry={entry} onOpen={onOpen} />
        {hoverOpen && <HoverPopover entry={entry} />}
      </div>
    )
  }

  // Full card — tier 2/3.
  return (
    <article
      ref={cardRef}
      data-testid="source-card"
      draggable
      onDragStart={handleDragStart}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="relative bg-surface border border-border rounded-md px-4 py-3 hover:shadow-card hover:border-border-strong transition-all group"
    >
      <DragHandle />

      <div className="flex items-start gap-3 mb-2">
        <span
          className={`text-[10px] font-mono px-1.5 py-0.5 rounded border shrink-0 mt-1 ${tierBadgeClass(
            entry.relevance_score,
          )}`}
          title="Relevance score"
        >
          tier {entry.relevance_score ?? '—'}
        </span>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-ink leading-snug">
            {entry.title || '(untitled)'}
          </h3>
          <div className="text-[11px] text-ink-muted mt-0.5 truncate">{meta.join(' · ')}</div>
        </div>
      </div>

      {/* Relevance WHY — the prominent preview-before-click signal. */}
      {entry.relevance_why && (
        <div className="border-l-2 border-accent bg-accent-light/40 pl-3 pr-2 py-1.5 my-2 rounded-r">
          <p className="text-[11px] uppercase tracking-wider text-accent font-semibold mb-0.5">
            Why
          </p>
          <p className="text-xs italic text-ink-secondary leading-relaxed line-clamp-3">
            {entry.relevance_why}
          </p>
        </div>
      )}

      {/* Abstract preview (collapsed → 3 lines). */}
      {entry.abstract && (
        <div className="mt-2 text-xs text-ink-secondary leading-relaxed">
          <p className={showAbstract ? '' : 'line-clamp-3'}>{entry.abstract}</p>
          {entry.abstract.length > 200 && (
            <button
              onClick={() => setShowAbstract(!showAbstract)}
              className="text-[11px] text-ink-muted hover:text-ink mt-0.5"
            >
              {showAbstract ? 'less' : 'more'}
            </button>
          )}
        </div>
      )}

      {/* FTS snippet — search-mode highlighted excerpt. */}
      {snippet && (
        <div className="mt-2 px-2 py-1.5 rounded bg-surface-muted border border-border/60">
          <p className="text-[11px] uppercase tracking-wider text-ink-muted font-semibold mb-0.5">
            Match
          </p>
          <p
            className="text-[11px] text-ink-secondary leading-relaxed"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderSnippetHtml(snippet) }}
          />
        </div>
      )}

      <div className="flex items-center gap-3 mt-3 pt-2 border-t border-border/70">
        <CardActions entry={entry} onOpen={onOpen} />
        {entry.cross_gap_refs.length > 0 && <CrossGapChip refs={entry.cross_gap_refs} />}
      </div>

      {hoverOpen && <HoverPopover entry={entry} />}
    </article>
  )
}

/** Small drag handle, only visible on card hover. */
function DragHandle() {
  return (
    <span
      className="absolute -left-4 top-2 opacity-0 group-hover:opacity-100 transition-opacity text-ink-muted cursor-grab active:cursor-grabbing"
      title="Drag to Word/Pages — drops Chicago citation"
    >
      <GripVertical size={14} />
    </span>
  )
}

/** Open + Cite + Star + Read action row. */
function CardActions({ entry, onOpen }: { entry: DossierEntry; onOpen: () => void }) {
  const hasOpenable = !!entry.pdf_path || !!entry.url
  const { isStarred, isRead, toggleStar, toggleRead } = useLibraryStore()
  const starred = isStarred(entry.id)
  const read = isRead(entry.id)
  return (
    <div className="flex items-center gap-2 ml-auto">
      <button
        onClick={onOpen}
        disabled={!hasOpenable}
        className="flex items-center gap-1 text-[11px] font-medium text-ink-secondary hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed"
        title={entry.pdf_path ? `Open PDF: ${entry.pdf_path}` : entry.url || 'No link'}
      >
        {entry.pdf_path ? <FileText size={12} /> : <ExternalLink size={12} />}
        {entry.pdf_path ? 'Open PDF' : 'Open URL'}
      </button>
      <CiteDropdown entry={entry} />
      <button
        onClick={() => {
          toggleStar(entry.id)
          showToast(starred ? 'Removed from queue' : 'Added to reading queue')
        }}
        className={`flex items-center gap-1 text-[11px] font-medium ${
          starred ? 'text-accent' : 'text-ink-secondary hover:text-ink'
        }`}
        title={starred ? 'Unstar' : 'Star (add to reading queue)'}
      >
        <Star size={12} fill={starred ? 'currentColor' : 'none'} />
      </button>
      <button
        onClick={() => {
          toggleRead(entry.id)
          showToast(read ? 'Marked unread' : 'Marked as read')
        }}
        className={`flex items-center gap-1 text-[11px] font-medium ${
          read ? 'text-emerald-600' : 'text-ink-secondary hover:text-ink'
        }`}
        title={read ? 'Mark unread' : 'Mark as read'}
      >
        <Eye size={12} />
      </button>
    </div>
  )
}

/** Cite dropdown menu — copies Chicago / short / link to clipboard. */
function CiteDropdown({ entry }: { entry: DossierEntry }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function onDoc(ev: MouseEvent) {
      if (!wrapRef.current?.contains(ev.target as Node)) setOpen(false)
    }
    if (open) document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [open])

  const copy = async (label: string, text: string) => {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(label)
      window.setTimeout(() => setCopied(null), 1200)
      const friendly =
        label === 'chicago'
          ? 'Chicago citation copied'
          : label === 'short'
          ? 'Short citation copied'
          : 'Link copied'
      showToast(friendly)
    } catch {
      /* clipboard refused — silently no-op; the user can drag instead. */
    }
  }

  // Memoised so we don't re-derive the strings on every render hover.
  const forms = useMemo(
    () => ({
      chicago: chicagoCitation(entry),
      short: shortCitation(entry),
      link: linkOnly(entry),
    }),
    [entry],
  )

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-[11px] font-medium text-ink-secondary hover:text-ink"
      >
        {copied ? <Check size={12} className="text-emerald-600" /> : <Copy size={12} />}
        Cite
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-30 bg-surface-card border border-border rounded-md shadow-panel py-1 w-56 text-xs">
          <button
            onClick={() => copy('chicago', forms.chicago)}
            className="w-full text-left px-3 py-1.5 hover:bg-surface-muted flex items-center gap-2"
          >
            <FileText size={11} className="text-ink-muted" />
            <span>Chicago</span>
          </button>
          <button
            onClick={() => copy('short', forms.short)}
            className="w-full text-left px-3 py-1.5 hover:bg-surface-muted flex items-center gap-2"
          >
            <Copy size={11} className="text-ink-muted" />
            <span>(Author Year)</span>
          </button>
          <button
            onClick={() => copy('link', forms.link)}
            disabled={!forms.link}
            className="w-full text-left px-3 py-1.5 hover:bg-surface-muted disabled:opacity-40 flex items-center gap-2"
          >
            <LinkIcon size={11} className="text-ink-muted" />
            <span>{entry.pdf_path ? 'PDF path' : 'URL'}</span>
          </button>
        </div>
      )}
    </div>
  )
}

/** Cross-gap reference chip — shows "Also in N gaps" with expand on click. */
function CrossGapChip({ refs }: { refs: string[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink"
      >
        <Network size={11} />
        Also in {refs.length} gap{refs.length !== 1 ? 's' : ''}
      </button>
      {open && (
        <div className="absolute left-0 bottom-full mb-1 z-30 bg-surface-card border border-border rounded-md shadow-panel p-2 w-64 text-[11px]">
          <p className="text-ink-muted uppercase tracking-wider font-semibold mb-1">
            Also relevant to
          </p>
          <div className="flex flex-wrap gap-1">
            {refs.map((id) => (
              <a
                key={id}
                href={`/write/gaps/${id}`}
                className="font-mono text-[10px] px-1.5 py-0.5 rounded border bg-surface-muted text-ink-secondary hover:text-ink"
              >
                {id}
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** Hover popover with the full WHY + abstract — "preview before clicking". */
function HoverPopover({ entry }: { entry: DossierEntry }) {
  return (
    <div className="absolute left-full top-0 ml-3 z-40 w-96 bg-surface-card border border-border rounded-md shadow-panel p-4 pointer-events-none">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-accent mb-1">
        Why this source
      </p>
      <p className="text-xs italic text-ink-secondary leading-relaxed mb-3">
        {entry.relevance_why || '(no relevance note)'}
      </p>
      {entry.abstract ? (
        <>
          <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-muted mb-1">
            Abstract
          </p>
          <p className="text-xs text-ink-secondary leading-relaxed">{entry.abstract}</p>
        </>
      ) : (
        <p className="text-[11px] text-ink-muted italic">No abstract available.</p>
      )}
      {(entry.doi || entry.url || entry.pdf_path) && (
        <div className="mt-3 pt-2 border-t border-border text-[10px] font-mono text-ink-muted break-all">
          {entry.doi && <div>doi: {entry.doi}</div>}
          {entry.pdf_path && <div>pdf: {entry.pdf_path}</div>}
          {entry.url && <div>url: {entry.url}</div>}
        </div>
      )}
    </div>
  )
}
