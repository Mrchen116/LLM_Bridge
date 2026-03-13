import type { TimelineEvent, TimelineResponse } from '../api/contracts'
import { formatToolArgsHint, normalizeReadableText } from './event-display'

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
  cardTitle: string
  cardSummary: string
  detailLoaded: boolean
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
  const cardTitle = buildCardTitle(event)
  const cardSummary = buildCardSummary(event, preview)

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
    cardTitle,
    cardSummary,
    detailLoaded: event.detail_loaded !== false,
    raw: event,
  }
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

function buildCardTitle(event: TimelineEvent): string {
  if (event.kind === 'tool_call') {
    const toolName = shortLine((event.tool_name ?? 'unknown').trim())
    return `Tool · ${toolName}`
  }
  return `Message · ${event.kind}`
}

function buildCardSummary(event: TimelineEvent, preview: string): string {
  if (event.kind === 'tool_call') {
    const toolHint = formatToolArgsHint(event)
    return shortLine(toolHint || preview || event.kind)
  }
  return shortLine(event.summary || preview || event.kind)
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
