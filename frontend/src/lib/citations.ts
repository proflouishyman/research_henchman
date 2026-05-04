// Citation helpers for the writing-companion drag-to-Word UX.
//
// Three forms are emitted per entry:
//   - chicagoCitation — full Chicago-ish footnote.
//   - shortCitation   — "(Last Year)" Author-Date short form.
//   - linkOnly        — pdf_path if present, else absolute URL.
//
// The drag dataTransfer attaches all three under different MIME types
// so the user can paste any of them into Word/Pages.

import type { DossierEntry } from '../types/library'

/** Best-effort first author last name for short citations. */
function firstAuthorLastName(authors: string): string {
  const raw = (authors || '').trim()
  if (!raw) return ''
  // Authors strings observed in the corpus include patterns like
  //   "Smith, John; Jones, Mary"   — semicolon-separated, last-first.
  //   "Khanna, Tarun"              — last, first.
  //   "John Smith"                 — first last (rare).
  const firstAuthor = raw.split(/[;]/)[0].trim()
  if (firstAuthor.includes(',')) {
    return firstAuthor.split(',')[0].trim()
  }
  // First-last form: take the last whitespace-separated token.
  const tokens = firstAuthor.split(/\s+/)
  return tokens[tokens.length - 1] || ''
}

/** Extract a 4-digit year from a freeform pub_date. */
function yearFromDate(pub_date: string): string {
  const m = (pub_date || '').match(/\d{4}/)
  return m ? m[0] : ''
}

/** Source-aware idiom hint — books vs journals vs newspapers vs filings. */
function sourceIdiom(source_id: string): 'book' | 'journal' | 'newspaper' | 'filing' | 'generic' {
  if (source_id === 'hathitrust_fulltext') return 'book'
  if (source_id === 'ebsco_api') return 'journal'
  if (source_id.startsWith('proquest_')) return 'newspaper'
  if (source_id === 'sec_edgar') return 'filing'
  return 'generic'
}

/**
 * Best-effort Chicago-style citation. Always single-line, no
 * trailing newline. The user is expected to fine-tune in Word — we
 * just give them a sensible starting string.
 */
export function chicagoCitation(entry: DossierEntry): string {
  const idiom = sourceIdiom(entry.source_id)
  const author = entry.authors?.trim() || ''
  const title = entry.title?.trim() || '(untitled)'
  const journal = entry.journal?.trim() || ''
  const year = yearFromDate(entry.pub_date) || entry.pub_date?.trim() || ''
  const url = entry.url?.trim()

  const parts: string[] = []
  if (author) parts.push(author + '.')

  if (idiom === 'journal') {
    parts.push(`"${title}."`)
    if (journal) parts.push(year ? `${journal} (${year}).` : `${journal}.`)
    else if (year) parts.push(`${year}.`)
  } else if (idiom === 'newspaper') {
    parts.push(`"${title}."`)
    if (journal) parts.push(year ? `${journal}, ${year}.` : `${journal}.`)
    else if (year) parts.push(`${year}.`)
  } else if (idiom === 'filing') {
    parts.push(`${title}.`)
    parts.push(`SEC EDGAR${year ? ', ' + year : ''}.`)
  } else if (idiom === 'book') {
    parts.push(`${title}.`)
    if (year) parts.push(`${year}.`)
  } else {
    parts.push(`${title}.`)
    if (journal) parts.push(`${journal}.`)
    if (year) parts.push(`${year}.`)
  }

  if (url) parts.push(url + '.')
  return parts.filter(Boolean).join(' ')
}

/** Compact (Last Year) short form — falls back to title when author missing. */
export function shortCitation(entry: DossierEntry): string {
  const last = firstAuthorLastName(entry.authors || '')
  const year = yearFromDate(entry.pub_date)
  if (last && year) return `(${last} ${year})`
  if (last) return `(${last})`
  if (year) return `(${year})`
  // Last-resort fallback so the user always gets *something* useful.
  return `("${(entry.title || '').slice(0, 40)}")`
}

/** PDF path if present, else URL. The "click-through" canonical link. */
export function linkOnly(entry: DossierEntry): string {
  if (entry.pdf_path) return entry.pdf_path
  return entry.url || ''
}

/**
 * Attach all three citation forms to a DragEvent's dataTransfer so the
 * user can drag from a SourceCard into Word/Pages and paste any form.
 */
export function attachDragCitations(ev: React.DragEvent, entry: DossierEntry): void {
  const chicago = chicagoCitation(entry)
  const short = shortCitation(entry)
  const link = linkOnly(entry)
  ev.dataTransfer.setData('text/plain', chicago)
  ev.dataTransfer.setData('text/x-citation-short', short)
  if (link) ev.dataTransfer.setData('text/uri-list', link)
  ev.dataTransfer.effectAllowed = 'copy'
}
