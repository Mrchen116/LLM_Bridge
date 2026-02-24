import type { SessionSummary, TimelineResponse } from '../api/contracts'

export interface TimelineFilters {
  agent: string
  tool: string
  q: string
  qNot: string
  includeNonTool: boolean
}

export interface InspectorState {
  sessions: SessionSummary[]
  sessionsNextCursor: string | null
  sessionsLoading: boolean
  sessionsError: string
  sessionQuery: string

  selectedSessionId: string
  selectedSessionDir: string

  timeline: TimelineResponse | null
  timelineLoading: boolean
  timelineError: string

  selectedEventId: string
  filters: TimelineFilters
}

export const defaultFilters: TimelineFilters = {
  agent: '',
  tool: '',
  q: '',
  qNot: '',
  includeNonTool: true,
}

export const initialInspectorState: InspectorState = {
  sessions: [],
  sessionsNextCursor: null,
  sessionsLoading: false,
  sessionsError: '',
  sessionQuery: '',

  selectedSessionId: '',
  selectedSessionDir: '',

  timeline: null,
  timelineLoading: false,
  timelineError: '',

  selectedEventId: '',
  filters: defaultFilters,
}
