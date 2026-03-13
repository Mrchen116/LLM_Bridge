import { memo } from 'react'
import type { ParsedTimelineEvent } from '../../lib/timeline-parser'

interface EventCardProps {
  event: ParsedTimelineEvent
  selected: boolean
  onSelect: (eventId: string) => void
}
function EventCardInner({ event, selected, onSelect }: EventCardProps) {
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
      <div className={`event-title ${event.kindClass}`} title={event.cardTitle}>
        {event.cardTitle}
      </div>
      {event.cardSummary ? (
        <div className="event-summary" title={event.cardSummary}>
          {event.cardSummary}
        </div>
      ) : null}
    </article>
  )
}

export const EventCard = memo(EventCardInner, (prevProps, nextProps) => {
  return (
    prevProps.event.eventId === nextProps.event.eventId &&
    prevProps.selected === nextProps.selected &&
    prevProps.onSelect === nextProps.onSelect
  )
})
