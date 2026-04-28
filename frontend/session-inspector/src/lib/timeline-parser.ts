import type { TimelineEvent, TimelineResponse } from '../api/contracts'
import { formatToolArgsHint, normalizeReadableText } from './event-display'

interface ParseTimelineOptions {
  expandTools?: boolean
}

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

export function parseTimelineResponse(
  timeline: TimelineResponse | null,
  options: ParseTimelineOptions = {},
): ParsedTimeline | null {
  if (!timeline) {
    return null
  }

  const events = options.expandTools === false ? groupToolEvents(timeline.events) : timeline.events

  return {
    sessionId: timeline.session_id,
    sessionDir: timeline.session_dir,
    lanes: timeline.lanes,
    events: events.map(parseTimelineEvent),
    stats: timeline.stats,
    meta: timeline.meta,
  }
}

export function parseTimelineEvent(event: TimelineEvent): ParsedTimelineEvent {
  const kindClass = event.kind === 'tool_call' || event.kind === 'tool_call_group' ? 'tool' : 'message'
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
  if (event.kind === 'tool_call_group' || event.kind === 'tool_result_group') {
    return event.summary || event.kind
  }
  if (event.kind === 'tool_call') {
    const toolName = shortLine((event.tool_name ?? 'unknown').trim())
    return `Tool · ${toolName}`
  }
  return `Message · ${event.kind}`
}

function buildCardSummary(event: TimelineEvent, preview: string): string {
  if (event.kind === 'tool_call_group' || event.kind === 'tool_result_group') {
    return ''
  }
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

function isGroupableToolKind(kind: string): kind is 'tool_call' | 'tool_result' {
  return kind === 'tool_call' || kind === 'tool_result'
}

function toolNameForSummary(event: TimelineEvent): string {
  const name = (event.tool_name ?? '').trim()
  return name || 'unknown'
}

function buildToolCallGroup(events: TimelineEvent[], groupIndex: number): TimelineEvent {
  const first = events[0]
  const toolNames = events.map(toolNameForSummary)
  const summary = `Tool(${events.length}) · ${toolNames.join(' ')}`
  return {
    ...first,
    event_id: `${first.turn_ts || first.ts}:tool_call_group:${groupIndex}`,
    kind: 'tool_call_group',
    summary,
    detail: { grouped_events: events },
    detail_loaded: true,
    tool_name: toolNames.join(' '),
    tool_args: null,
    tool_def: null,
    group_count: events.length,
    grouped_events: events,
    representative_event_id: first.event_id,
  }
}

function buildToolResultGroup(events: TimelineEvent[], groupIndex: number): TimelineEvent {
  const first = events[0]
  return {
    ...first,
    event_id: `${first.turn_ts || first.ts}:tool_result_group:${groupIndex}`,
    kind: 'tool_result_group',
    summary: `Message · tool_result(${events.length})`,
    detail: { grouped_events: events },
    detail_loaded: true,
    tool_name: null,
    tool_args: null,
    tool_def: null,
    group_count: events.length,
    grouped_events: events,
    representative_event_id: first.event_id,
  }
}

function flushToolGroup(
  output: TimelineEvent[],
  group: TimelineEvent[],
  groupIndex: number,
): number {
  if (group.length === 0) {
    return groupIndex
  }
  if (group.length === 1) {
    output.push(group[0])
    return groupIndex
  }

  const kind = group[0].kind
  if (kind === 'tool_call') {
    output.push(buildToolCallGroup(group, groupIndex))
    return groupIndex + 1
  }
  if (kind === 'tool_result') {
    output.push(buildToolResultGroup(group, groupIndex))
    return groupIndex + 1
  }

  output.push(...group)
  return groupIndex
}

function canJoinToolGroup(current: TimelineEvent[], next: TimelineEvent): boolean {
  if (current.length === 0) {
    return isGroupableToolKind(next.kind)
  }
  const first = current[0]
  return (
    next.kind === first.kind &&
    next.lane_id === first.lane_id &&
    next.turn_ts === first.turn_ts
  )
}

export function groupToolEvents(events: TimelineEvent[]): TimelineEvent[] {
  const output: TimelineEvent[] = []
  let currentGroup: TimelineEvent[] = []
  let groupIndex = 0

  for (const event of events) {
    if (isGroupableToolKind(event.kind) && canJoinToolGroup(currentGroup, event)) {
      currentGroup.push(event)
      continue
    }

    groupIndex = flushToolGroup(output, currentGroup, groupIndex)
    currentGroup = isGroupableToolKind(event.kind) ? [event] : []
    if (!isGroupableToolKind(event.kind)) {
      output.push(event)
    }
  }

  flushToolGroup(output, currentGroup, groupIndex)
  return output
}
