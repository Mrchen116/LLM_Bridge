import { describe, expect, it } from 'vitest'
import type { TimelineResponse } from '../../api/contracts'
import { formatTimestampLabel, groupToolEvents, parseTimelineResponse } from '../timeline-parser'

function buildTimelineResponse(): TimelineResponse {
  return {
    session_id: 'demo-session',
    session_dir: '2026-demo-session',
    lanes: [
      {
        lane_id: 'agent.alpha',
        label: 'Agent Alpha',
        event_count: 2,
        first_ts: '2026-02-22_12-00-00_000',
        last_ts: '2026-02-22_12-00-01_000',
      },
    ],
    events: [
      {
        event_id: '1',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'run command',
        detail: { ok: true },
        tool_name: 'Bash',
        tool_args: { cmd: 'ls' },
        tool_def: { name: 'Bash' },
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'openai_chat',
      },
      {
        event_id: '2',
        ts: '2026-02-22_12-00-01_000',
        lane_id: 'agent.alpha',
        kind: 'assistant_text',
        summary: 'done',
        detail: { text: 'done' },
        turn_ts: '2026-02-22_12-00-01_000',
        format: 'openai_chat',
      },
    ],
    stats: {
      total_events: 2,
      tool_events: 1,
      non_tool_events: 1,
      lane_count: 1,
    },
    meta: {
      warnings: [],
      summary_chars: 120,
    },
  }
}

describe('timeline-parser', () => {
  it('formats timeline events with stable view fields', () => {
    const parsed = parseTimelineResponse(buildTimelineResponse())

    expect(parsed).not.toBeNull()
    expect(parsed?.events).toHaveLength(2)
    expect(parsed?.events[0].kindClass).toBe('tool')
    expect(parsed?.events[0].preview).toBe('run command')
    expect(parsed?.events[0].timestampLabel).toBe('2026-02-22 12-00-00_000')
    expect(parsed?.events[1].kindClass).toBe('message')
    expect(parsed?.events[1].preview).toBe('done')
  })

  it('formats timestamp labels', () => {
    expect(formatTimestampLabel('')).toBe('')
    expect(formatTimestampLabel('short')).toBe('short')
    expect(formatTimestampLabel('2026-02-22_12-00-00_000')).toBe('2026-02-22 12-00-00_000')
  })

  it('keeps tool events expanded by default', () => {
    const timeline = buildTimelineResponse()
    timeline.events = [
      {
        event_id: 'tool-1',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Read',
        detail: {},
        tool_name: 'Read',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'tool-2',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Bash',
        detail: {},
        tool_name: 'Bash',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
    ]

    const parsed = parseTimelineResponse(timeline)

    expect(parsed?.events.map((event) => event.kind)).toEqual(['tool_call', 'tool_call'])
  })

  it('groups contiguous tool calls and tool results when tools are collapsed', () => {
    const timeline = buildTimelineResponse()
    timeline.events = [
      {
        event_id: 'result-1',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_result',
        summary: 'README',
        detail: { summary_text: 'README' },
        tool_name: 'Read',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'result-2',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_result',
        summary: 'listing',
        detail: { summary_text: 'listing' },
        tool_name: 'Bash',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'text-1',
        ts: '2026-02-22_12-00-00_500',
        lane_id: 'agent.alpha',
        kind: 'assistant_text',
        summary: 'working',
        detail: { content: 'working' },
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'tool-1',
        ts: '2026-02-22_12-00-01_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Read',
        detail: {},
        tool_name: 'Read',
        turn_ts: '2026-02-22_12-00-01_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'tool-2',
        ts: '2026-02-22_12-00-01_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Read',
        detail: {},
        tool_name: 'Read',
        turn_ts: '2026-02-22_12-00-01_000',
        format: 'anthropic_messages',
      },
      {
        event_id: 'tool-3',
        ts: '2026-02-22_12-00-01_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Bash',
        detail: {},
        tool_name: 'Bash',
        turn_ts: '2026-02-22_12-00-01_000',
        format: 'anthropic_messages',
      },
    ]

    const parsed = parseTimelineResponse(timeline, { expandTools: false })

    expect(parsed?.events.map((event) => event.kind)).toEqual([
      'tool_result_group',
      'assistant_text',
      'tool_call_group',
    ])
    expect(parsed?.events[0].cardTitle).toBe('Message · tool_result(2)')
    expect(parsed?.events[0].raw.grouped_events).toHaveLength(2)
    expect(parsed?.events[2].cardTitle).toBe('Tool(3) · Read Read Bash')
    expect(parsed?.events[2].raw.grouped_events?.map((event) => event.event_id)).toEqual([
      'tool-1',
      'tool-2',
      'tool-3',
    ])
  })

  it('does not group singleton tool events', () => {
    const [event] = groupToolEvents([
      {
        event_id: 'tool-1',
        ts: '2026-02-22_12-00-00_000',
        lane_id: 'agent.alpha',
        kind: 'tool_call',
        summary: 'Read',
        detail: {},
        tool_name: 'Read',
        turn_ts: '2026-02-22_12-00-00_000',
        format: 'anthropic_messages',
      },
    ])

    expect(event.kind).toBe('tool_call')
    expect(event.event_id).toBe('tool-1')
  })
})
