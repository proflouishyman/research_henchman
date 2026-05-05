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
  GripVertical,
} from 'lucide-react'
// Note: Eye import removed — C12: Read toggle hidden from UI.
// The underlying read state in library.ts is preserved for future use.
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
  // Phase 4: Internet Archive
  internet_archive: 'Internet Archive',
  internet_archive_ia_html: 'Internet Archive',
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

/**
 * Phase 1: Availability badge for a source card entry.
 *
 * Color scheme:
 *   Blue  — Local PDF on disk (most actionable)
 *   Green — Full view (HathiTrust or IA public domain)
 *   Yellow— Library only (HathiTrust/IA limited/restricted + no PDF)
 *   Orange— Cloud (has URL, not categorized above)
 *   Gray  — No link available
 *
 * For Internet Archive records, label reads "IA · Full view" or "IA · Library only".
 */
function AvailabilityBadge({ entry }: { entry: DossierEntry }) {
  const isIA = entry.source_id === 'internet_archive' || entry.source_id === 'internet_archive_ia_html'
  const isHathi = entry.source_id === 'hathitrust_fulltext'
  const prefix = isIA ? 'IA · ' : ''

  if (entry.pdf_path) {
    return (
      <span
        data-testid="availability-badge"
        className="text-[9px] font-medium px-1 py-0.5 rounded border bg-blue-50 text-blue-600 border-blue-200"
        title="Local PDF available"
      >
        Local PDF
      </span>
    )
  }

  if ((isHathi || isIA) && entry.access === 'Full view') {
    return (
      <span
        data-testid="availability-badge"
        className="text-[9px] font-medium px-1 py-0.5 rounded border bg-emerald-50 text-emerald-600 border-emerald-200"
        title="Freely readable online"
      >
        {prefix}Full view
      </span>
    )
  }

  if ((isHathi || isIA) && entry.access?.startsWith('Limited') && !entry.pdf_path) {
    return (
      <span
        data-testid="availability-badge"
        className="text-[9px] font-medium px-1 py-0.5 rounded border bg-amber-50 text-amber-600 border-amber-200"
        title="Accessible via library subscription"
      >
        {prefix}Library only
      </span>
    )
  }

  if (entry.url) {
    return (
      <span
        data-testid="availability-badge"
        className="text-[9px] font-medium px-1 py-0.5 rounded border bg-orange-50 text-orange-500 border-orange-200"
        title="Available online"
      >
        Cloud
      </span>
    )
  }

  return null  // no link of any kind — show nothing
}

/**
 * Phase 1: "Look up at JHU" and "Try Internet Archive" inline search links.
 * Only shown for non-PDF, non-Full-view sources (where we can't open directly).
 */
function LibrarySearchLinks({ entry }: { entry: DossierEntry }) {
  const hasPdf = !!entry.pdf_path
  const isFullView = entry.access === 'Full view'
  // Show these links only when the source isn't directly readable.
  if (hasPdf || isFullView) return null

  const title   = encodeURIComponent(entry.title || '')
  const authors = encodeURIComponent(entry.authors || '')

  const jhuUrl = `https://catalyst.library.jhu.edu/discovery/search?query=any,contains,${title}+${authors}&tab=Everything&search_scope=MyInst_and_CI&vid=01JHU_INST:01JHU`
  const iaUrl  = `https://archive.org/search.php?query=title%3A%22${title}%22+creator%3A%22${authors}%22`

  return (
    <div className="flex items-center gap-2 mt-1">
      <a
        href={jhuUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[10px] text-ink-muted hover:text-accent underline"
        title="Search JHU Catalyst"
      >
        Look up at JHU
      </a>
      <a
        href={iaUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[10px] text-ink-muted hover:text-accent underline"
        title="Search Internet Archive"
      >
        Try Internet Archive
      </a>
    </div>
  )
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
    // D14: Dismiss the onboarding drag hint on first drag.
    try { window.localStorage.setItem(DRAG_SEEN_KEY, '1') } catch { /* ignore */ }
  }

  // B13: also_in_sources moved to hover popover only — main meta line keeps
  // just primary source. This declutters the card's single info row.
  const meta: string[] = []
  if (entry.authors) meta.push(entry.authors)
  if (entry.pub_date) meta.push(entry.pub_date)
  meta.push(srcLabel(entry.source_id))

  // Phase 2: from_gap_id breadcrumb (set on cross-linked AUTO-* entries).
  const fromGapId = entry.from_gap_id || null

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
          <div className="text-xs text-ink truncate flex items-center gap-1.5">
            <span className="font-medium">{entry.title || '(untitled)'}</span>
            <span className="text-ink-muted"> — {meta.join(' · ')}</span>
            <AvailabilityBadge entry={entry} />
          </div>
          {fromGapId && (
            <div className="text-[10px] text-ink-muted mt-0.5">
              from {fromGapId}
            </div>
          )}
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
        {/* B9: renamed from "tier N" → "score N" to reserve "tier" for gap-level badges. */}
        <span
          className={`text-[10px] font-mono px-1.5 py-0.5 rounded border shrink-0 mt-1 ${tierBadgeClass(
            entry.relevance_score,
          )}`}
          title="Relevance score (0=false positive, 3=cite-worthy)"
        >
          score {entry.relevance_score ?? '—'}
        </span>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-ink leading-snug">
            {entry.title || '(untitled)'}
          </h3>
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted mt-0.5">
            <span className="truncate">{meta.join(' · ')}</span>
            <AvailabilityBadge entry={entry} />
          </div>
          {/* Phase 2: from_gap_id breadcrumb for cross-linked AUTO-* entries. */}
          {fromGapId && (
            <div className="text-[10px] text-ink-muted mt-0.5">
              from {fromGapId}
            </div>
          )}
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

      <div className="mt-3 pt-2 border-t border-border/70">
        <div className="flex items-center gap-3">
          <CardActions entry={entry} onOpen={onOpen} />
          {entry.cross_gap_refs.length > 0 && <CrossGapChip refs={entry.cross_gap_refs} />}
        </div>
        {/* Phase 1: JHU and IA search links for non-directly-readable sources. */}
        <LibrarySearchLinks entry={entry} />
      </div>

      {hoverOpen && <HoverPopover entry={entry} />}
    </article>
  )
}

const DRAG_SEEN_KEY = 'library.onboarding.drag_seen'

/**
 * Small drag handle with improved discoverability.
 *
 * B5: opacity-40 at rest (was opacity-0) so it's always faintly visible.
 * D14: On first hover, a small tooltip emerges from the handle. Dismissed
 *   on first drag-start, × click, or after 5 seconds. Persists to
 *   localStorage['library.onboarding.drag_seen'].
 */
function DragHandle() {
  const [showHint, setShowHint] = useState(false)
  const hintTimer = useRef<number | null>(null)
  // Check once whether the user has seen the hint.
  const alreadySeen =
    typeof window !== 'undefined' && !!window.localStorage.getItem(DRAG_SEEN_KEY)

  const dismissHint = () => {
    setShowHint(false)
    if (hintTimer.current) window.clearTimeout(hintTimer.current)
    try {
      window.localStorage.setItem(DRAG_SEEN_KEY, '1')
    } catch { /* ignore */ }
  }

  const onMouseEnter = () => {
    if (alreadySeen) return
    const seen = window.localStorage.getItem(DRAG_SEEN_KEY)
    if (seen) return
    setShowHint(true)
    hintTimer.current = window.setTimeout(dismissHint, 5000)
  }

  useEffect(() => {
    return () => {
      if (hintTimer.current) window.clearTimeout(hintTimer.current)
    }
  }, [])

  return (
    <span
      className="absolute -left-4 top-2 opacity-40 group-hover:opacity-100 transition-opacity text-ink-muted cursor-grab active:cursor-grabbing"
      title="Drag to Word — drops Chicago citation"
      onMouseEnter={onMouseEnter}
      onMouseLeave={() => {
        // Don't hide while the hint is showing — let the timer or × handle it.
      }}
    >
      <GripVertical size={14} />
      {showHint && (
        <div
          className="absolute left-5 top-0 z-50 flex items-center gap-2 bg-ink text-surface rounded-md px-3 py-1.5 text-[11px] whitespace-nowrap shadow-panel pointer-events-auto"
          style={{ minWidth: 220 }}
        >
          <span>Drag to Word — drops Chicago citation</span>
          <button
            onClick={(e) => { e.stopPropagation(); dismissHint() }}
            className="ml-1 opacity-70 hover:opacity-100 font-bold text-xs"
            title="Dismiss"
          >
            ×
          </button>
        </div>
      )}
    </span>
  )
}

/**
 * Open + Cite (3 inline buttons) + Star action row.
 *
 * Phase 1: Replaces the CiteDropdown with three inline icon buttons for
 * Chicago, Short, and Link. One click + toast. No dropdown.
 *
 * A4: If entry has BOTH a pdf_path AND a url, the primary CTA opens the PDF
 *   and a secondary "(or open URL)" link is shown.
 * C12: Read toggle (Eye icon) removed from UI. The underlying read state in
 *   library.ts is preserved; only the render is removed.
 */
function CardActions({ entry, onOpen }: { entry: DossierEntry; onOpen: () => void }) {
  const hasPdf = !!entry.pdf_path
  const hasUrl = !!entry.url
  const hasOpenable = hasPdf || hasUrl
  const { isStarred, toggleStar } = useLibraryStore()
  const starred = isStarred(entry.id)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  // A4: secondary URL handler for when the primary CTA opens the PDF.
  const onOpenUrl = () => {
    if (entry.url) window.open(entry.url, '_blank', 'noopener,noreferrer')
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

  const copyForm = async (key: string, text: string, label: string) => {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey(null), 1200)
      showToast(label)
    } catch {
      /* clipboard refused — silently no-op; the user can drag instead */
    }
  }

  return (
    <div className="flex items-center gap-2 ml-auto flex-wrap">
      {/* A4: Primary open button — prefers PDF when available. */}
      <button
        onClick={onOpen}
        disabled={!hasOpenable}
        className="flex items-center gap-1 text-[11px] font-medium text-ink-secondary hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed"
        title={hasPdf ? `Open PDF: ${entry.pdf_path}` : entry.url || 'No link'}
      >
        {hasPdf ? <FileText size={12} /> : <ExternalLink size={12} />}
        {hasPdf ? 'Open PDF' : 'Open URL'}
      </button>
      {/* A4: Secondary URL link when both PDF and URL exist. */}
      {hasPdf && hasUrl && (
        <button
          onClick={onOpenUrl}
          className="text-[10px] text-ink-muted hover:text-ink underline"
          title={`Open original URL: ${entry.url}`}
        >
          or open URL
        </button>
      )}
      {/* Phase 1: Three inline citation buttons replacing the CiteDropdown. */}
      <button
        onClick={() => copyForm('chicago', forms.chicago, 'Chicago citation copied')}
        className="flex items-center gap-0.5 text-[11px] font-medium text-ink-secondary hover:text-ink"
        title="Copy Chicago citation"
        aria-label="Copy Chicago citation"
      >
        {copiedKey === 'chicago' ? <Check size={11} className="text-emerald-600" /> : <FileText size={11} />}
      </button>
      <button
        onClick={() => copyForm('short', forms.short, 'Short citation copied')}
        className="flex items-center gap-0.5 text-[11px] font-medium text-ink-secondary hover:text-ink"
        title="Copy short citation (Author Year)"
        aria-label="Copy short citation"
      >
        {copiedKey === 'short' ? <Check size={11} className="text-emerald-600" /> : <Copy size={11} />}
      </button>
      <button
        onClick={() => copyForm('link', forms.link, 'Link copied')}
        disabled={!forms.link}
        className="flex items-center gap-0.5 text-[11px] font-medium text-ink-secondary hover:text-ink disabled:opacity-40"
        title={entry.pdf_path ? 'Copy PDF path' : 'Copy URL'}
        aria-label="Copy link"
      >
        {copiedKey === 'link' ? <Check size={11} className="text-emerald-600" /> : <LinkIcon size={11} />}
      </button>
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
    </div>
  )
}

// CiteDropdown replaced in Phase 1 by three inline icon buttons in CardActions.
// The citation logic is unchanged — chicagoCitation / shortCitation / linkOnly
// helpers are still used directly inside CardActions.

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

/** Hover popover with the full WHY + abstract — "preview before clicking".
 * B13: also_in_sources rendered here as "Also in: HathiTrust, EBSCO."
 */
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
      {/* B13: also_in_sources shown in hover popover instead of inline meta. */}
      {entry.also_in_sources.length > 0 && (
        <p className="mt-2 text-[10px] text-ink-muted">
          Also in: {entry.also_in_sources.map(srcLabel).join(', ')}
        </p>
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
