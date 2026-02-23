import type { ParsedTimelineEvent } from '../../lib/timeline-parser'
import { formatToolArgsPreview } from '../../lib/event-display'

interface EventCardProps {
  event: ParsedTimelineEvent
  laneLabel: string
  selected: boolean
  onSelect: (eventId: string) => void
}

function shortLane(value: string): string {
  const text = value.trim()
  if (text.length <= 18) {
    return text
  }
  return `${text.slice(0, 18)}...`
}

function buildToolCallHint(event: ParsedTimelineEvent): string {
  const text = formatToolArgsPreview(event.raw).trim()
  if (!text) {
    return ''
  }
  const firstLine = text.split('\n')[0]?.trim() || ''
  if (!firstLine) {
    return ''
  }
  return firstLine.length <= 72 ? firstLine : `${firstLine.slice(0, 72)}...`
}

export function EventCard({ event, laneLabel, selected, onSelect }: EventCardProps) {
  const toolHint = event.kind === 'tool_call' ? buildToolCallHint(event) : ''

  return (
    <article
      className={`event-card ${selected ? 'active' : ''}`}
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
      <div className="event-card-topline">
        <span>{event.kindLabel}</span>
        <span title={laneLabel}>{shortLane(laneLabel)}</span>
      </div>
      <div className={`event-kind ${event.kindClass}`}>{event.kind}</div>
      <div className="event-summary" title={event.preview}>
        {event.preview}
      </div>
      {toolHint ? (
        <div className="event-hint" title={toolHint}>
          {toolHint}
        </div>
      ) : null}
    </article>
  )
}
