import type { TimelineEvent } from '../api/contracts'

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function normalizeEscapedText(value: string): string {
  return value
    .replace(/\r\n/g, '\n')
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
}

export function normalizeReadableText(value: string): string {
  return normalizeEscapedText(value).replace(/\\"/g, '"')
}

export function extractEventMainText(event: Pick<TimelineEvent, 'kind' | 'summary' | 'detail'>): string {
  const detail = event.detail

  if (event.kind === 'user_input' && isObjectRecord(detail)) {
    const summaryText = detail.summary_text
    if (typeof summaryText === 'string') {
      return normalizeReadableText(summaryText)
    }
  }

  if (event.kind === 'assistant_text' && isObjectRecord(detail)) {
    const content = detail.content
    if (typeof content === 'string') {
      return normalizeReadableText(content)
    }
  }

  if (event.kind === 'assistant_reasoning' && isObjectRecord(detail)) {
    const reasoning = detail.reasoning_content
    if (typeof reasoning === 'string') {
      return normalizeReadableText(reasoning)
    }
  }

  if (typeof detail === 'string') {
    return normalizeReadableText(detail)
  }

  if (typeof event.summary === 'string' && event.summary.trim()) {
    return normalizeReadableText(event.summary)
  }

  if (detail !== undefined && detail !== null) {
    return formatCodeValue(detail)
  }

  return ''
}

export function formatCodeValue(value: unknown): string {
  if (typeof value === 'string') {
    return normalizeEscapedText(value)
  }

  try {
    return normalizeEscapedText(JSON.stringify(value, null, 2))
  } catch {
    return String(value)
  }
}

export function formatToolArgsPreview(event: TimelineEvent): string {
  if (event.kind !== 'tool_call') {
    return ''
  }
  if (event.tool_args == null) {
    return ''
  }
  return formatCodeValue(event.tool_args)
}
