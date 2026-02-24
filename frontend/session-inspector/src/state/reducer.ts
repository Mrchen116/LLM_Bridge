import type { InspectorAction } from './actions'
import { initialInspectorState, type InspectorState } from './types'

export function inspectorReducer(
  state: InspectorState,
  action: InspectorAction,
): InspectorState {
  switch (action.type) {
    case 'SET_SESSION_QUERY':
      return {
        ...state,
        sessionQuery: action.payload,
      }
    case 'SESSIONS_LOADING':
      return {
        ...state,
        sessionsLoading: true,
        sessionsError: '',
      }
    case 'SESSIONS_LOADED':
      return {
        ...state,
        sessions: action.payload.items,
        sessionsNextCursor: action.payload.nextCursor,
        sessionsLoading: false,
        sessionsError: '',
      }
    case 'SESSIONS_FAILED':
      return {
        ...state,
        sessions: [],
        sessionsLoading: false,
        sessionsError: action.payload,
      }
    case 'SELECT_SESSION':
      return {
        ...state,
        selectedSessionId: action.payload.sessionId,
        selectedSessionDir: action.payload.sessionDir,
        selectedEventId: '',
        timelineError: '',
      }
    case 'TIMELINE_LOADING':
      return {
        ...state,
        timelineLoading: true,
        timelineError: '',
      }
    case 'TIMELINE_LOADED':
      return {
        ...state,
        timeline: action.payload,
        timelineLoading: false,
        timelineError: '',
        selectedEventId: '',
      }
    case 'TIMELINE_FAILED':
      return {
        ...state,
        timeline: null,
        timelineLoading: false,
        timelineError: action.payload,
        selectedEventId: '',
      }
    case 'SET_FILTER':
      return {
        ...state,
        filters: {
          ...state.filters,
          [action.payload.key]: action.payload.value,
        },
        selectedEventId: '',
      }
    case 'SET_FILTERS':
      return {
        ...state,
        filters: {
          ...state.filters,
          ...action.payload,
        },
        selectedEventId: '',
      }
    case 'SELECT_EVENT':
      return {
        ...state,
        selectedEventId: action.payload,
      }
    default:
      return state
  }
}

export { initialInspectorState }
