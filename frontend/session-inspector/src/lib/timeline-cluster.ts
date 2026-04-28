import type { TimelineLane } from '../api/contracts'
import type { ParsedTimeline, ParsedTimelineEvent } from './timeline-parser'

export interface SelectOption {
  value: string
  label: string
}

export interface TimelineLaneCluster {
  lane: TimelineLane
  events: ParsedTimelineEvent[]
}

export interface TimelineGridCell {
  laneId: string
  event: ParsedTimelineEvent | null
}

export interface TimelineGridRow {
  eventId: string
  laneId: string
  laneIndex: number
  timestampLabel: string
  event: ParsedTimelineEvent
}

export interface TimelineGrid {
  laneOrder: TimelineLane[]
  laneIndexById: Record<string, number>
  rows: TimelineGridRow[]
}

export function clusterEventsByLane(parsed: ParsedTimeline | null): TimelineLaneCluster[] {
  if (!parsed) {
    return []
  }

  const eventsByLane = new Map<string, ParsedTimelineEvent[]>()
  for (const event of parsed.events) {
    const existing = eventsByLane.get(event.laneId)
    if (existing) {
      existing.push(event)
      continue
    }
    eventsByLane.set(event.laneId, [event])
  }

  return parsed.lanes
    .filter((lane) => eventsByLane.has(lane.lane_id))
    .map((lane) => ({
      lane,
      events: eventsByLane.get(lane.lane_id) ?? [],
    }))
}

export function buildTimelineGrid(parsed: ParsedTimeline | null): TimelineGrid {
  if (!parsed) {
    return {
      laneOrder: [],
      laneIndexById: {},
      rows: [],
    }
  }

  const activeLaneIds = new Set(parsed.events.map((event) => event.laneId))
  const laneOrder = parsed.lanes.filter((lane) => activeLaneIds.has(lane.lane_id))
  const laneIndexById = Object.fromEntries(
    laneOrder.map((lane, index) => [lane.lane_id, index]),
  ) as Record<string, number>

  if (!laneOrder.length) {
    return {
      laneOrder,
      laneIndexById,
      rows: [],
    }
  }

  const rows: TimelineGridRow[] = parsed.events.map((event) => ({
    eventId: event.eventId,
    laneId: event.laneId,
    laneIndex: laneIndexById[event.laneId] ?? -1,
    timestampLabel: event.timestampLabel,
    event,
  }))

  return {
    laneOrder,
    laneIndexById,
    rows,
  }
}

export function buildTimelineFilterOptions(parsed: ParsedTimeline | null): {
  laneOptions: SelectOption[]
  toolOptions: SelectOption[]
} {
  const laneOptions: SelectOption[] = [{ value: '', label: '全部 Agent' }]
  const toolOptions: SelectOption[] = [{ value: '', label: '全部工具' }]

  if (!parsed) {
    return { laneOptions, toolOptions }
  }

  for (const lane of parsed.lanes) {
    laneOptions.push({ value: lane.lane_id, label: lane.label })
  }

  const toolSet = new Set<string>()
  for (const event of parsed.events) {
    if (event.kind === 'tool_call' && event.raw.tool_name) {
      toolSet.add(event.raw.tool_name)
    }
    if (event.kind === 'tool_call_group' && Array.isArray(event.raw.grouped_events)) {
      for (const child of event.raw.grouped_events) {
        if (child.tool_name) {
          toolSet.add(child.tool_name)
        }
      }
    }
  }

  for (const toolName of [...toolSet].sort((a, b) => a.localeCompare(b))) {
    toolOptions.push({ value: toolName, label: toolName })
  }

  return {
    laneOptions,
    toolOptions,
  }
}
