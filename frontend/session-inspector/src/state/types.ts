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
  sessionsLoading: boolean
  sessionsError: string
  sessionQuery: string
  sessionPage: number
  sessionPageSize: number
  sessionsTotalItems: number
  sessionsTotalPages: number
  sessionsHasPrev: boolean
  sessionsHasNext: boolean

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
  sessionsLoading: false,
  sessionsError: '',
  sessionQuery: '',
  sessionPage: 1,
  sessionPageSize: 50,
  sessionsTotalItems: 0,
  sessionsTotalPages: 1,
  sessionsHasPrev: false,
  sessionsHasNext: false,

  selectedSessionId: '',
  selectedSessionDir: '',

  timeline: null,
  timelineLoading: false,
  timelineError: '',

  selectedEventId: '',
  filters: defaultFilters,
}
