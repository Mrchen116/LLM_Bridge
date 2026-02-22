import type { SessionSummary, TimelineResponse } from '../api/contracts'
import type { TimelineFilters } from './types'

export type FilterKey = keyof TimelineFilters

export type InspectorAction =
  | { type: 'SET_SESSION_QUERY'; payload: string }
  | { type: 'SESSIONS_LOADING' }
  | { type: 'SESSIONS_LOADED'; payload: { items: SessionSummary[]; nextCursor: string | null } }
  | { type: 'SESSIONS_FAILED'; payload: string }
  | { type: 'SELECT_SESSION'; payload: { sessionId: string; sessionDir: string } }
  | { type: 'TIMELINE_LOADING' }
  | { type: 'TIMELINE_LOADED'; payload: TimelineResponse }
  | { type: 'TIMELINE_FAILED'; payload: string }
  | { type: 'SET_FILTER'; payload: { key: FilterKey; value: TimelineFilters[FilterKey] } }
  | { type: 'SELECT_EVENT'; payload: string }

export const inspectorActions = {
  setSessionQuery: (query: string): InspectorAction => ({
    type: 'SET_SESSION_QUERY',
    payload: query,
  }),
  sessionsLoading: (): InspectorAction => ({ type: 'SESSIONS_LOADING' }),
  sessionsLoaded: (items: SessionSummary[], nextCursor: string | null): InspectorAction => ({
    type: 'SESSIONS_LOADED',
    payload: { items, nextCursor },
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
  selectEvent: (eventId: string): InspectorAction => ({
    type: 'SELECT_EVENT',
    payload: eventId,
  }),
}
