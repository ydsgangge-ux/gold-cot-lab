import { ReactNode } from 'react'
import { GLOSSARY, TERM_BY_KEY } from '../glossary'

interface TermProps {
  /** Key into the glossary. Unknown keys render their children unchanged. */
  k: string
  children?: ReactNode
}

/**
 * A label that carries its own explanation.
 *
 * The hint is a native `title`, so it works with no state, no portal and no
 * layout shift — and the same wording is repeated in full in `GlossaryPanel`
 * for anyone who would rather read than hover.
 */
export function Term({ k, children }: TermProps) {
  const entry = TERM_BY_KEY[k]
  if (!entry) return <>{children ?? k}</>
  return (
    <span className="term" title={`${entry.plain}\n${entry.reading}`}>
      {children ?? entry.term}
    </span>
  )
}

/** The same explanations, laid out where a first-time reader will look for them. */
export function GlossaryPanel() {
  return (
    <details className="glossary">
      <summary>这些词是什么意思？（点开看对照表）</summary>
      <dl className="glossary-list">
        {GLOSSARY.map((entry) => (
          <div key={entry.key} className="glossary-item">
            <dt>{entry.term}</dt>
            <dd>
              {entry.plain}
              <span className="glossary-reading">{entry.reading}</span>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  )
}