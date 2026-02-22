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
  timestampLabel: string
  cells: TimelineGridCell[]
}

export interface TimelineGrid {
  laneOrder: TimelineLane[]
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
  const clusters = clusterEventsByLane(parsed)
  const laneOrder = clusters.map((cluster) => cluster.lane)

  if (!laneOrder.length || !parsed) {
    return {
      laneOrder,
      rows: [],
    }
  }

  const rows: TimelineGridRow[] = parsed.events.map((event) => ({
    eventId: event.eventId,
    timestampLabel: event.timestampLabel,
    cells: laneOrder.map((lane) => ({
      laneId: lane.lane_id,
      event: lane.lane_id === event.laneId ? event : null,
    })),
  }))

  return {
    laneOrder,
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
  }

  for (const toolName of [...toolSet].sort((a, b) => a.localeCompare(b))) {
    toolOptions.push({ value: toolName, label: toolName })
  }

  return {
    laneOptions,
    toolOptions,
  }
}
