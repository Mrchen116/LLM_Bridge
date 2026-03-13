import { describe, expect, it } from 'vitest'
import type { TimelineResponse } from '../../api/contracts'
import {
  buildTimelineFilterOptions,
  buildTimelineGrid,
  clusterEventsByLane,
} from '../timeline-cluster'
import { parseTimelineResponse } from '../timeline-parser'

function buildTimelineResponse(): TimelineResponse {
  return {
    session_id: 'demo-session',
    session_dir: '2026-demo-session',
    lanes: [
      {
        lane_id: 'agent.alpha',
        label: 'Agent Alpha',
        event_count: 1,
        first_ts: '2026-02-22_12-00-00_000',
        last_ts: '2026-02-22_12-00-00_000',
      },
      {
        lane_id: 'agent.beta',
        label: 'Agent Beta',
        event_count: 1,
        first_ts: '2026-02-22_12-00-01_000',
        last_ts: '2026-02-22_12-00-01_000',
      },
      {
        lane_id: 'agent.empty',
        label: 'Agent Empty',
        event_count: 0,
        first_ts: '',
        last_ts: '',
      },
    ],
    events: [
      {
        event_id: '1',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'run command',
        detail: {},
        tool_name: 'ReadFile',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'openai_chat',
      },
      {
        event_id: '2',
        ts: '2026-02-22_12-00-01_000',
        lane_id: 'agent.beta',
        kind: 'tool_call',
        summary: 'run command',
        detail: {},
        tool_name: 'Bash',
        turn_ts: '2026-02-22_12-00-01_000',
        format: 'openai_chat',
      },
      {
        event_id: '3',
        ts: '2026-02-22_12-00-02_000',
        lane_id: 'agent.beta',
        kind: 'assistant_text',
        summary: 'done',
        detail: {},
        turn_ts: '2026-02-22_12-00-02_000',
        format: 'openai_chat',
      },
    ],
    stats: {
      total_events: 3,
      tool_events: 2,
      non_tool_events: 1,
      lane_count: 3,
    },
    meta: {
      warnings: [],
      summary_chars: 120,
    },
  }
}

describe('timeline-cluster', () => {
  it('clusters events by non-empty lanes in backend order', () => {
    const parsed = parseTimelineResponse(buildTimelineResponse())
    const clusters = clusterEventsByLane(parsed)

    expect(clusters.map((item) => item.lane.lane_id)).toEqual(['agent.alpha', 'agent.beta'])
    expect(clusters[0].events).toHaveLength(1)
    expect(clusters[1].events).toHaveLength(2)
  })

  it('builds lane-aligned grid rows for timeline rendering', () => {
    const parsed = parseTimelineResponse(buildTimelineResponse())
    const grid = buildTimelineGrid(parsed)

    expect(grid.laneOrder).toHaveLength(2)
    expect(grid.rows).toHaveLength(3)
    expect(grid.rows[0].event.eventId).toBe('1')
    expect(grid.rows[0].laneIndex).toBe(0)
    expect(grid.rows[1].event.eventId).toBe('2')
    expect(grid.rows[1].laneIndex).toBe(1)
    expect(grid.laneIndexById['agent.alpha']).toBe(0)
    expect(grid.laneIndexById['agent.beta']).toBe(1)
  })

  it('builds filter options with de-duplicated and sorted tool names', () => {
    const parsed = parseTimelineResponse(buildTimelineResponse())
    const options = buildTimelineFilterOptions(parsed)

    expect(options.laneOptions).toHaveLength(4)
    expect(options.toolOptions.map((item) => item.value)).toEqual(['', 'Bash', 'ReadFile'])
  })
})
