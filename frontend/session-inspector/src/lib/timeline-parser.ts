import type { TimelineEvent, TimelineResponse } from '../api/contracts'

export interface ParsedTimelineEvent {
  eventId: string
  laneId: string
  timestamp: string
  timestampLabel: string
  kind: string
  kindLabel: string
  kindClass: 'tool' | 'message'
  summary: string
  preview: string
  raw: TimelineEvent
}

export interface ParsedTimeline {
  sessionId: string
  sessionDir: string
  lanes: TimelineResponse['lanes']
  events: ParsedTimelineEvent[]
  stats: TimelineResponse['stats']
  meta: TimelineResponse['meta']
}

export function parseTimelineResponse(timeline: TimelineResponse | null): ParsedTimeline | null {
  if (!timeline) {
    return null
  }

  return {
    sessionId: timeline.session_id,
    sessionDir: timeline.session_dir,
    lanes: timeline.lanes,
    events: timeline.events.map(parseTimelineEvent),
    stats: timeline.stats,
    meta: timeline.meta,
  }
}

export function parseTimelineEvent(event: TimelineEvent): ParsedTimelineEvent {
  const kindClass = event.kind === 'tool_call' ? 'tool' : 'message'
  const kindLabel = kindClass === 'tool' ? 'Tool' : 'Message'
  const preview = event.summary || (kindClass === 'tool' ? event.tool_name || event.kind : event.kind)

  return {
    eventId: event.event_id,
    laneId: event.lane_id,
    timestamp: event.ts,
    timestampLabel: formatTimestampLabel(event.ts),
    kind: event.kind,
    kindLabel,
    kindClass,
    summary: event.summary,
    preview,
    raw: event,
  }
}

export function formatTimestampLabel(timestamp: string): string {
  if (!timestamp) {
    return ''
  }

  if (timestamp.length <= 12) {
    return timestamp
  }

  return timestamp.replace('_', ' ')
}
