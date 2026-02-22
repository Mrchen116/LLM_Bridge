import type { SessionsResponse, TimelineResponse } from './contracts'
import type { TimelineFilters } from '../state/types'

const API_BASE = '/api/session-inspector'

async function requestJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
    },
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }

  return (await response.json()) as T
}

export async function fetchSessions(query: string, limit = 80): Promise<SessionsResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))

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
): Promise<TimelineResponse> {
  const params = new URLSearchParams()
  params.set('include_non_tool', filters.includeNonTool ? 'true' : 'false')
  params.set('summary_chars', String(summaryChars))

  if (filters.agent) {
    params.set('agent', filters.agent)
  }
  if (filters.tool) {
    params.set('tool', filters.tool)
  }
  if (filters.q) {
    params.set('q', filters.q)
  }

  return requestJson<TimelineResponse>(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/timeline?${params.toString()}`,
  )
}
