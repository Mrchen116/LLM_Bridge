import { describe, expect, it } from 'vitest'
import type { TimelineResponse } from '../../api/contracts'
import { formatTimestampLabel, parseTimelineResponse } from '../timeline-parser'

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
})
