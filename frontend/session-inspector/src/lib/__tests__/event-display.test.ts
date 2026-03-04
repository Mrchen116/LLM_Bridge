import { describe, expect, it } from 'vitest'
import type { TimelineEvent } from '../../api/contracts'
import {
  extractEventMainText,
  formatCodeValue,
  formatToolArgsHint,
  formatToolArgsPreview,
  normalizeEscapedText,
} from '../event-display'

function buildEvent(partial: Partial<TimelineEvent>): TimelineEvent {
  return {
    event_id: 'ev-1',
    ts: '2026-02-22_15-56-38_958',
    lane_id: 'lane-1',
    kind: 'assistant_text',
    summary: '',
    detail: {},
    turn_ts: '2026-02-22_15-56-38_958',
    format: 'anthropic_messages',
    ...partial,
  }
}

describe('event-display', () => {
  it('extracts user_input text from summary_text field', () => {
    const event = buildEvent({
      kind: 'user_input',
      summary: 'fallback',
      detail: { summary_text: 'line1\\nline2' },
    })
    expect(extractEventMainText(event)).toBe('line1\nline2')
  })

  it('extracts tool_result text from summary_text field', () => {
    const event = buildEvent({
      kind: 'tool_result',
      summary: '{"name":"task"}...',
      detail: { summary_text: '{"name":"task","output":{"message":"full text"}}' },
    })
    expect(extractEventMainText(event)).toBe('{"name":"task","output":{"message":"full text"}}')
  })

  it('extracts assistant text payload from detail.content', () => {
    const event = buildEvent({
      kind: 'assistant_text',
      detail: { content: 'tool said: \\"done\\"' },
    })
    expect(extractEventMainText(event)).toBe('tool said: "done"')
  })

  it('formats tool args preview from tool_call payload', () => {
    const event = buildEvent({
      kind: 'tool_call',
      tool_args: { cmd: 'printf "a\\\\nb"' },
    })
    const text = formatToolArgsPreview(event)
    expect(text).toContain('"cmd"')
    expect(text).toContain('printf')
    expect(text).toContain('\n')
  })

  it('builds tool hint from meaningful prioritized keys', () => {
    const event = buildEvent({
      kind: 'tool_call',
      tool_args: {
        meta: { ignore: true },
        team_name: 'ops',
      },
    })
    expect(formatToolArgsHint(event)).toBe('team_name=ops')
  })

  it('skips isolated braces when deriving tool hint', () => {
    const event = buildEvent({
      kind: 'tool_call',
      tool_args: '{\n}',
    })
    expect(formatToolArgsHint(event)).toBe('')
  })

  it('renders structured value with decoded escaped newline', () => {
    const text = formatCodeValue({ description: 'first line\\nsecond line' })
    expect(text).toContain('first line')
    expect(text).toContain('second line')
    expect(text).toContain('\n')
  })

  it('decodes unicode escape sequences for readable display', () => {
    expect(normalizeEscapedText('hello: \\u4f60\\u597d')).toBe('hello: 你好')
  })

  it('decodes double-escaped unicode escape sequences (JSON-within-JSON)', () => {
    expect(normalizeEscapedText('hello: \\\\u4f60\\\\u597d')).toBe('hello: 你好')
  })

  it('decodes surrogate pairs when present', () => {
    expect(normalizeEscapedText('icon=\\uD83D\\uDE00')).toBe('icon=😀')
  })

  it('decodes double-escaped surrogate pairs when present', () => {
    expect(normalizeEscapedText('icon=\\\\uD83D\\\\uDE00')).toBe('icon=😀')
  })

  it('preserves control unicode escapes instead of emitting control characters', () => {
    expect(normalizeEscapedText('control=\\u0002')).toBe('control=\\u0002')
  })

  it('renders double-escaped newline without leaving a stray backslash', () => {
    expect(normalizeEscapedText('a\\\\nb')).toBe('a\nb')
  })
})
