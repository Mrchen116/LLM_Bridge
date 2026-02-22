import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import type { TimelineEvent } from '../api/contracts'
import { fetchSessions, fetchTimeline } from '../api/session-inspector-client'
import {
  buildTimelineFilterOptions,
  buildTimelineGrid,
  clusterEventsByLane,
} from '../lib/timeline-cluster'
import { parseTimelineResponse } from '../lib/timeline-parser'
import { inspectorActions } from '../state/actions'
import { initialInspectorState, inspectorReducer } from '../state/reducer'
import type { FilterKey } from '../state/actions'
import type { TimelineFilters } from '../state/types'

const SESSION_SEARCH_DEBOUNCE_MS = 180
const TIMELINE_SUMMARY_CHARS = 120

function toErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  return String(error)
}

export function useSessionInspectorController() {
  const [state, dispatch] = useReducer(inspectorReducer, initialInspectorState)

  const sessionsRequestId = useRef(0)
  const timelineRequestId = useRef(0)

  const loadSessions = useCallback(async (query: string) => {
    const requestId = sessionsRequestId.current + 1
    sessionsRequestId.current = requestId
    dispatch(inspectorActions.sessionsLoading())

    try {
      const payload = await fetchSessions(query, 80)
      if (sessionsRequestId.current !== requestId) {
        return
      }

      dispatch(inspectorActions.sessionsLoaded(payload.items ?? [], payload.next_cursor ?? null))
    } catch (error) {
      if (sessionsRequestId.current !== requestId) {
        return
      }
      dispatch(inspectorActions.sessionsFailed(toErrorMessage(error)))
    }
  }, [])

  const loadTimeline = useCallback(async (sessionId: string, filters: TimelineFilters) => {
    const requestId = timelineRequestId.current + 1
    timelineRequestId.current = requestId
    dispatch(inspectorActions.timelineLoading())

    try {
      const payload = await fetchTimeline(sessionId, filters, TIMELINE_SUMMARY_CHARS)
      if (timelineRequestId.current !== requestId) {
        return
      }
      dispatch(inspectorActions.timelineLoaded(payload))
    } catch (error) {
      if (timelineRequestId.current !== requestId) {
        return
      }
      dispatch(inspectorActions.timelineFailed(toErrorMessage(error)))
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadSessions(state.sessionQuery)
    }, SESSION_SEARCH_DEBOUNCE_MS)

    return () => {
      window.clearTimeout(timer)
    }
  }, [loadSessions, state.sessionQuery])

  useEffect(() => {
    if (!state.selectedSessionId && state.sessions.length > 0) {
      const firstSession = state.sessions[0]
      dispatch(inspectorActions.selectSession(firstSession.session_id, firstSession.session_dir))
    }
  }, [state.selectedSessionId, state.sessions])

  useEffect(() => {
    if (!state.selectedSessionId) {
      return
    }
    void loadTimeline(state.selectedSessionId, state.filters)
  }, [loadTimeline, state.filters, state.selectedSessionId])

  const parsedTimeline = useMemo(() => parseTimelineResponse(state.timeline), [state.timeline])
  const laneClusters = useMemo(() => clusterEventsByLane(parsedTimeline), [parsedTimeline])
  const timelineGrid = useMemo(() => buildTimelineGrid(parsedTimeline), [parsedTimeline])
  const filterOptions = useMemo(() => buildTimelineFilterOptions(parsedTimeline), [parsedTimeline])

  const selectedEvent = useMemo<TimelineEvent | null>(() => {
    if (!state.timeline || !state.selectedEventId) {
      return null
    }

    return state.timeline.events.find((event) => event.event_id === state.selectedEventId) ?? null
  }, [state.selectedEventId, state.timeline])

  const setSessionQuery = useCallback((query: string) => {
    dispatch(inspectorActions.setSessionQuery(query))
  }, [])

  const selectSession = useCallback((sessionId: string, sessionDir: string) => {
    dispatch(inspectorActions.selectSession(sessionId, sessionDir))
  }, [])

  const setFilter = useCallback(
    <K extends FilterKey>(key: K, value: TimelineFilters[K]) => {
      dispatch(inspectorActions.setFilter(key, value))
    },
    [],
  )

  const selectEvent = useCallback((eventId: string) => {
    dispatch(inspectorActions.selectEvent(eventId))
  }, [])

  const refreshSessions = useCallback(() => {
    void loadSessions(state.sessionQuery)
  }, [loadSessions, state.sessionQuery])

  const refreshTimeline = useCallback(() => {
    if (!state.selectedSessionId) {
      return
    }
    void loadTimeline(state.selectedSessionId, state.filters)
  }, [loadTimeline, state.filters, state.selectedSessionId])

  return {
    state,
    parsedTimeline,
    timelineGrid,
    filterOptions,
    selectedEvent,
    activeLaneCount: laneClusters.length,
    actions: {
      setSessionQuery,
      selectSession,
      setFilter,
      selectEvent,
      refreshSessions,
      refreshTimeline,
    },
  }
}
