import type {
  KeywordPresetsResponse,
  LogFileContentResponse,
  RequestCopyCompressionResponse,
  SessionsResponse,
  TimelineEventDetailResponse,
  TimelineResponse,
  TokenBreakdownResponse,
} from './contracts'
import type { TimelineFilters } from '../state/types'

const API_BASE = '/api/session-inspector'

async function requestJson<T>(url: string): Promise<T> {
  const requestStartedAt = performance.now()
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
    },
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }

  const jsonStartedAt = performance.now()
  const payload = (await response.json()) as T
  const jsonEndedAt = performance.now()

  if (typeof window !== 'undefined') {
    console.info('[session-inspector] request timing', {
      url,
      fetchMs: Number((jsonStartedAt - requestStartedAt).toFixed(2)),
      jsonParseMs: Number((jsonEndedAt - jsonStartedAt).toFixed(2)),
      totalMs: Number((jsonEndedAt - requestStartedAt).toFixed(2)),
    })
  }

  return payload
}

async function requestJsonWithBody<T>(url: string, method: 'PUT', body: unknown): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }

  return (await response.json()) as T
}

async function requestJsonWithAnyBody<T>(url: string, method: 'POST', body: unknown): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }

  return (await response.json()) as T
}

export async function fetchSessions(query: string, page: number, limit = 50): Promise<SessionsResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  params.set('page', String(page))

  const normalizedQuery = query.trim()
  if (normalizedQuery) {
    params.set('q', normalizedQuery)
  }

  return requestJson<SessionsResponse>(`${API_BASE}/sessions?${params.toString()}`)
}

export async function fetchTimeline(
  sessionId: string,
  filters: TimelineFilters,
  summaryChars = 120,
  includeDetail = false,
): Promise<TimelineResponse> {
  const params = new URLSearchParams()
  params.set('include_non_tool', filters.includeNonTool ? 'true' : 'false')
  params.set('summary_chars', String(summaryChars))
  params.set('include_detail', includeDetail ? 'true' : 'false')

  if (filters.agent) {
    params.set('agent', filters.agent)
  }
  if (filters.tool) {
    params.set('tool', filters.tool)
  }
  if (filters.q) {
    params.set('q', filters.q)
  }
  if (filters.qNot) {
    params.set('q_not', filters.qNot)
  }

  return requestJson<TimelineResponse>(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/timeline?${params.toString()}`,
  )
}

export async function fetchTimelineEventDetail(
  sessionId: string,
  eventId: string,
  summaryChars = 120,
): Promise<TimelineEventDetailResponse> {
  const params = new URLSearchParams()
  params.set('summary_chars', String(summaryChars))
  return requestJson<TimelineEventDetailResponse>(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(eventId)}?${params.toString()}`,
  )
}

export async function fetchLogFileContent(path: string): Promise<LogFileContentResponse> {
  const params = new URLSearchParams()
  params.set('path', path)
  return requestJson<LogFileContentResponse>(`${API_BASE}/log-file?${params.toString()}`)
}

export async function fetchTokenBreakdown(
  sessionId: string,
  eventId: string,
): Promise<TokenBreakdownResponse> {
  return requestJson<TokenBreakdownResponse>(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(eventId)}/token-breakdown`,
  )
}

export async function fetchRequestCopyCompression(
  sessionId: string,
  eventId: string,
  thresholdTokens = 500,
): Promise<RequestCopyCompressionResponse> {
  const params = new URLSearchParams()
  params.set('threshold_tokens', String(thresholdTokens))
  return requestJson<RequestCopyCompressionResponse>(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(eventId)}/request-copy-compression?${params.toString()}`,
  )
}

export async function fetchKeywordPresets(): Promise<KeywordPresetsResponse> {
  return requestJson<KeywordPresetsResponse>(`${API_BASE}/keyword-presets`)
}

export async function saveKeywordPresets(payload: KeywordPresetsResponse): Promise<KeywordPresetsResponse> {
  return requestJsonWithBody<KeywordPresetsResponse>(`${API_BASE}/keyword-presets`, 'PUT', payload)
}

export async function fetchTextTokenCount(text: string): Promise<number> {
  const payload = await requestJsonWithAnyBody<{ tokens: number }>(`${API_BASE}/text-token-count`, 'POST', {
    text,
  })
  return Number(payload.tokens || 0)
}
