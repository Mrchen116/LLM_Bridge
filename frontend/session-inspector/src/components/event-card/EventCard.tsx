import type { ParsedTimelineEvent } from '../../lib/timeline-parser'
import { formatToolArgsHint, normalizeReadableText } from '../../lib/event-display'

interface EventCardProps {
  event: ParsedTimelineEvent
  selected: boolean
  onSelect: (eventId: string) => void
}

function normalizeCardLine(value: string): string {
  return normalizeReadableText(value).replace(/\s+/g, ' ').trim()
}

function shortLine(value: string, maxLength = 120): string {
  const text = normalizeCardLine(value)
  if (text.length <= maxLength) {
    return text
  }
  return `${text.slice(0, maxLength)}...`
}

function buildCardTitle(event: ParsedTimelineEvent): string {
  if (event.kind === 'tool_call') {
    const toolName = shortLine(event.raw.tool_name?.trim() || 'unknown')
    return `Tool · ${toolName}`
  }
  return `Message · ${event.kind}`
}

function buildSummary(event: ParsedTimelineEvent, toolHint: string): string {
  if (event.kind === 'tool_call') {
    return shortLine(toolHint)
  }
  const candidate = event.summary || event.preview || event.kind
  return shortLine(candidate)
}

export function EventCard({ event, selected, onSelect }: EventCardProps) {
  const toolHint = event.kind === 'tool_call' ? formatToolArgsHint(event.raw) : ''
  const title = buildCardTitle(event)
  const summary = buildSummary(event, toolHint)

  return (
    <article
      className={`event-card dense ${selected ? 'active' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(event.eventId)}
      onKeyDown={(inputEvent) => {
        if (inputEvent.key === 'Enter' || inputEvent.key === ' ') {
          inputEvent.preventDefault()
          onSelect(event.eventId)
        }
      }}
    >
      <div className={`event-title ${event.kindClass}`} title={title}>
        {title}
      </div>
      {summary ? (
        <div className="event-summary" title={summary}>
          {summary}
        </div>
      ) : null}
    </article>
  )
}
