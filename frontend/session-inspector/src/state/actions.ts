import type { SessionSummary, TimelineResponse } from '../api/contracts'
import type { TimelineFilters } from './types'

export type FilterKey = keyof TimelineFilters

export type InspectorAction =
  | { type: 'SET_SESSION_QUERY'; payload: string }
  | { type: 'SET_SESSION_PAGE'; payload: number }
  | { type: 'SESSIONS_LOADING' }
  | {
      type: 'SESSIONS_LOADED'
      payload: {
        items: SessionSummary[]
        page: number
        pageSize: number
        totalItems: number
        totalPages: number
        hasPrev: boolean
        hasNext: boolean
      }
    }
  | { type: 'SESSIONS_FAILED'; payload: string }
  | { type: 'SELECT_SESSION'; payload: { sessionId: string; sessionDir: string } }
  | { type: 'TIMELINE_LOADING' }
  | { type: 'TIMELINE_LOADED'; payload: TimelineResponse }
  | { type: 'TIMELINE_FAILED'; payload: string }
  | { type: 'SET_FILTER'; payload: { key: FilterKey; value: TimelineFilters[FilterKey] } }
  | { type: 'SET_FILTERS'; payload: Partial<TimelineFilters> }
  | { type: 'SELECT_EVENT'; payload: string }

export const inspectorActions = {
  setSessionQuery: (query: string): InspectorAction => ({
    type: 'SET_SESSION_QUERY',
    payload: query,
  }),
  setSessionPage: (page: number): InspectorAction => ({
    type: 'SET_SESSION_PAGE',
    payload: page,
  }),
  sessionsLoading: (): InspectorAction => ({ type: 'SESSIONS_LOADING' }),
  sessionsLoaded: (
    items: SessionSummary[],
    page: number,
    pageSize: number,
    totalItems: number,
    totalPages: number,
    hasPrev: boolean,
    hasNext: boolean,
  ): InspectorAction => ({
    type: 'SESSIONS_LOADED',
    payload: { items, page, pageSize, totalItems, totalPages, hasPrev, hasNext },
  }),
  sessionsFailed: (message: string): InspectorAction => ({
    type: 'SESSIONS_FAILED',
    payload: message,
  }),
  selectSession: (sessionId: string, sessionDir: string): InspectorAction => ({
    type: 'SELECT_SESSION',
    payload: { sessionId, sessionDir },
  }),
  timelineLoading: (): InspectorAction => ({ type: 'TIMELINE_LOADING' }),
  timelineLoaded: (timeline: TimelineResponse): InspectorAction => ({
    type: 'TIMELINE_LOADED',
    payload: timeline,
  }),
  timelineFailed: (message: string): InspectorAction => ({
    type: 'TIMELINE_FAILED',
    payload: message,
  }),
  setFilter: <K extends FilterKey>(key: K, value: TimelineFilters[K]): InspectorAction => ({
    type: 'SET_FILTER',
    payload: { key, value },
  }),
  setFilters: (filters: Partial<TimelineFilters>): InspectorAction => ({
    type: 'SET_FILTERS',
    payload: filters,
  }),
  selectEvent: (eventId: string): InspectorAction => ({
    type: 'SELECT_EVENT',
    payload: eventId,
  }),
}
