import type { ParsedTimelineEvent } from '../../lib/timeline-parser'

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

function stringifyInline(event: ParsedTimelineEvent): string {
  try {
    if (event.raw.kind === 'tool_call') {
      return JSON.stringify(event.raw.tool_args ?? {}, null, 2)
    }
    return JSON.stringify(event.raw.detail ?? {}, null, 2)
  } catch {
    return String(event.raw.summary || event.raw.kind)
  }
}

export function EventCard({ event, laneLabel, selected, onSelect }: EventCardProps) {
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

      {selected ? <pre className="code event-inline">{stringifyInline(event)}</pre> : null}
    </article>
  )
}
