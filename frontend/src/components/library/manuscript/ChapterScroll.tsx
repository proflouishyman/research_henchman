// ChapterScroll — renders all paragraphs in a single chapter, section by section.
//
// Props:
//   chapter     — the ManuscriptChapter to render
//   selectedParaId — highlighted para (from URL / store)
//   onGapClick  — fires when a gap badge or paragaraph row is clicked

import type { ManuscriptChapter } from '../../../types/library'
import { ParagraphRow } from './ParagraphRow'

interface Props {
  chapter: ManuscriptChapter
  selectedParaId: string | null
  onGapClick: (gapId: string, paraId: string) => void
}

export function ChapterScroll({ chapter, selectedParaId, onGapClick }: Props) {
  return (
    <div className="py-2">
      {/* Chapter title banner */}
      <div className="px-4 py-3 border-b border-border mb-2">
        <h2 className="text-lg font-bold text-ink">{chapter.title}</h2>
      </div>

      {chapter.sections.map((section, si) => (
        <div key={si} className="mb-4">
          {/* Section paragraphs */}
          {section.paragraphs.map((para) => (
            <div key={para.para_id} id={`para-${para.para_id}`}>
              <ParagraphRow
                para={para}
                isSelected={para.para_id === selectedParaId}
                onGapClick={(gapId) => onGapClick(gapId, para.para_id)}
              />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
