import type { TimelineEvent } from '../api/contracts'

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const TOOL_HINT_PRIORITY_KEYS = [
  'agent_type',
  'team_name',
  'task',
  'query',
  'cmd',
  'command',
  'path',
  'file',
  'filename',
  'url',
  'name',
]

function normalizeInlineText(value: string): string {
  return normalizeReadableText(value).replace(/\s+/g, ' ').trim()
}

function isScalar(value: unknown): value is string | number | boolean {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

function formatHintScalar(value: string | number | boolean): string {
  if (typeof value === 'string') {
    return normalizeInlineText(value)
  }
  return String(value)
}

function shortenHintLine(value: string, maxLength = 88): string {
  const text = value.trim()
  if (text.length <= maxLength) {
    return text
  }
  return `${text.slice(0, maxLength)}...`
}

function sanitizeHintText(value: string): string {
  const compact = normalizeInlineText(value)
  if (!compact || compact === '{' || compact === '}' || compact === '[' || compact === ']') {
    return ''
  }
  if (compact === '{ }' || compact === '[ ]') {
    return ''
  }
  return shortenHintLine(compact)
}

function toHintPair(key: string, value: unknown): string {
  if (!isScalar(value)) {
    return ''
  }
  const formattedValue = formatHintScalar(value)
  if (!formattedValue) {
    return ''
  }
  return shortenHintLine(`${key}=${formattedValue}`)
}

function findHintInValue(value: unknown): string {
  if (isScalar(value)) {
    return sanitizeHintText(formatHintScalar(value))
  }

  if (Array.isArray(value)) {
    for (const item of value) {
      const nested = findHintInValue(item)
      if (nested) {
        return nested
      }
    }
    return ''
  }

  if (!isObjectRecord(value)) {
    return ''
  }

  for (const key of TOOL_HINT_PRIORITY_KEYS) {
    if (!(key in value)) {
      continue
    }
    const formatted = toHintPair(key, value[key])
    if (formatted) {
      return formatted
    }
  }

  for (const [key, nestedValue] of Object.entries(value)) {
    const formatted = toHintPair(key, nestedValue)
    if (formatted) {
      return formatted
    }
    const nested = findHintInValue(nestedValue)
    if (nested) {
      return nested
    }
  }

  return ''
}

function sanitizeStructuredLine(line: string): string {
  const trimmed = line.trim().replace(/,$/, '')
  if (!trimmed || trimmed === '{' || trimmed === '}' || trimmed === '[' || trimmed === ']') {
    return ''
  }

  const keyValueLine = trimmed.match(/^"([^"]+)":\s*"?(.*?)"?$/)
  if (keyValueLine) {
    const key = keyValueLine[1]
    const value = keyValueLine[2].replace(/"$/, '').trim()
    if (value) {
      return shortenHintLine(`${key}=${normalizeInlineText(value)}`)
    }
  }

  return shortenHintLine(trimmed)
}

export function normalizeEscapedText(value: string): string {
  const normalized = value
    .replace(/\r\n/g, '\n')
    .replace(/\\\\r\\\\n/g, '\n')
    .replace(/\\r\\n/g, '\n')
    .replace(/\\\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\\\t/g, '\t')
    .replace(/\\t/g, '\t')

  return decodeUnicodeEscapes(normalized)
}

export function normalizeReadableText(value: string): string {
  return normalizeEscapedText(value).replace(/\\"/g, '"')
}

function decodeUnicodeEscapes(value: string): string {
  if (!value.includes('\\u')) {
    return value
  }

  const decodeUnit = (match: string, hex: string): string => {
    const codeUnit = Number.parseInt(hex, 16)
    if (codeUnit === 0x000a || codeUnit === 0x000d) {
      return '\n'
    }
    if (codeUnit === 0x0009) {
      return '\t'
    }
    if (codeUnit < 0x20) {
      return match
    }
    return String.fromCharCode(codeUnit)
  }

  const decodeCodePoint = (match: string, hex: string): string => {
    if (!/^[0-9a-fA-F]{1,6}$/.test(hex)) {
      return match
    }
    const codePoint = Number.parseInt(hex, 16)
    if (codePoint < 0x20 || codePoint > 0x10ffff || (codePoint >= 0xd800 && codePoint <= 0xdfff)) {
      return match
    }
    return String.fromCodePoint(codePoint)
  }

  const decodeSurrogatePair = (match: string, highHex: string, lowHex: string): string => {
    const high = Number.parseInt(highHex, 16)
    const low = Number.parseInt(lowHex, 16)
    if (!(high >= 0xd800 && high <= 0xdbff && low >= 0xdc00 && low <= 0xdfff)) {
      return match
    }
    const codePoint = 0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00)
    return String.fromCodePoint(codePoint)
  }

  // Decode double-escaped sequences first (common when viewing JSON-within-JSON).
  const withDoubleDecoded = value
    .replace(/\\\\u(d[89ab][0-9a-f]{2})\\\\u(d[c-f][0-9a-f]{2})/gi, decodeSurrogatePair)
    .replace(/\\\\u\\{([0-9a-fA-F]{1,6})\\}/g, decodeCodePoint)
    .replace(/\\\\u([0-9a-fA-F]{4})/g, decodeUnit)

  return withDoubleDecoded
    .replace(/\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})/gi, decodeSurrogatePair)
    .replace(/\\u\\{([0-9a-fA-F]{1,6})\\}/g, decodeCodePoint)
    .replace(/\\u([0-9a-fA-F]{4})/g, decodeUnit)
}

export function extractEventMainText(event: Pick<TimelineEvent, 'kind' | 'summary' | 'detail'>): string {
  const detail = event.detail

  if ((event.kind === 'user_input' || event.kind === 'tool_result') && isObjectRecord(detail)) {
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

export function formatToolArgsHint(event: TimelineEvent): string {
  if (event.kind !== 'tool_call' || event.tool_args == null) {
    return ''
  }

  const prioritized = findHintInValue(event.tool_args)
  if (prioritized) {
    return prioritized
  }

  const lines = formatCodeValue(event.tool_args)
    .split('\n')
    .map((line) => sanitizeStructuredLine(line))
    .filter(Boolean)

  return lines[0] ?? ''
}
