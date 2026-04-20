import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import type { TimelineEvent } from '../api/contracts'
import type { KeywordPreset, KeywordPresetsResponse } from '../api/contracts'
import {
  fetchKeywordPresets,
  fetchSessions,
  fetchTimeline,
  saveKeywordPresets,
} from '../api/session-inspector-client'
import {
  buildTimelineFilterOptions,
  buildTimelineGrid,
} from '../lib/timeline-cluster'
import { parseTimelineResponse } from '../lib/timeline-parser'
import { inspectorActions } from '../state/actions'
import { initialInspectorState, inspectorReducer } from '../state/reducer'
import type { FilterKey } from '../state/actions'
import type { TimelineFilters } from '../state/types'

const SESSION_SEARCH_DEBOUNCE_MS = 180
const TIMELINE_SUMMARY_CHARS = 120

function logPerf(label: string, payload: Record<string, unknown>) {
  if (typeof window === 'undefined') {
    return
  }
  console.info(`[session-inspector] ${label}`, payload)
}

function toErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  return String(error)
}

export function useSessionInspectorController() {
  const [state, dispatch] = useReducer(inspectorReducer, initialInspectorState)
  const [keywordPresetState, setKeywordPresetState] = useState<KeywordPresetsResponse>({
    version: 1,
    default_preset_id: null,
    presets: [],
  })
  const [selectedKeywordPresetId, setSelectedKeywordPresetId] = useState<string>('')

  const sessionsRequestId = useRef(0)
  const timelineRequestId = useRef(0)

  const loadSessions = useCallback(async (query: string, page: number) => {
    const requestId = sessionsRequestId.current + 1
    sessionsRequestId.current = requestId
    dispatch(inspectorActions.sessionsLoading())
    const startedAt = performance.now()

    try {
      const payload = await fetchSessions(query, page, state.sessionPageSize)
      if (sessionsRequestId.current !== requestId) {
        return
      }

       logPerf('sessions loaded', {
        page: payload.page,
        pageSize: payload.page_size,
        totalMs: Number((performance.now() - startedAt).toFixed(2)),
        serverPerf: payload.meta?.perf ?? null,
        serverCache: payload.meta?.cache ?? null,
        counts: payload.meta?.counts ?? null,
      })

      dispatch(
        inspectorActions.sessionsLoaded(
          payload.items ?? [],
          payload.page,
          payload.page_size,
          payload.total_items,
          payload.total_pages,
          payload.has_prev,
          payload.has_next,
        ),
      )
    } catch (error) {
      if (sessionsRequestId.current !== requestId) {
        return
      }
      dispatch(inspectorActions.sessionsFailed(toErrorMessage(error)))
    }
  }, [state.sessionPageSize])

  const loadTimeline = useCallback(async (sessionId: string, filters: TimelineFilters) => {
    const requestId = timelineRequestId.current + 1
    timelineRequestId.current = requestId
    dispatch(inspectorActions.timelineLoading())
    const startedAt = performance.now()

    try {
      const payload = await fetchTimeline(sessionId, filters, TIMELINE_SUMMARY_CHARS, false)
      if (timelineRequestId.current !== requestId) {
        return
      }
      logPerf('timeline loaded', {
        sessionId,
        totalMs: Number((performance.now() - startedAt).toFixed(2)),
        serverPerf: payload.meta?.perf ?? null,
        serverCache: payload.meta?.cache ?? null,
        totalEvents: payload.stats.total_events,
        laneCount: payload.stats.lane_count,
      })
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
      void loadSessions(state.sessionQuery, state.sessionPage)
    }, SESSION_SEARCH_DEBOUNCE_MS)

    return () => {
      window.clearTimeout(timer)
    }
  }, [loadSessions, state.sessionPage, state.sessionQuery])

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

  const applyPresetToFilters = useCallback((preset: KeywordPreset) => {
    dispatch(
      inspectorActions.setFilters({
        q: preset.include_keywords.join(', '),
        qNot: preset.exclude_keywords.join(', '),
      }),
    )
  }, [])

  const loadKeywordPresets = useCallback(async () => {
    try {
      const payload = await fetchKeywordPresets()
      setKeywordPresetState(payload)
      const defaultPreset =
        payload.presets.find((preset) => preset.id === payload.default_preset_id) ?? payload.presets[0]
      if (defaultPreset) {
        setSelectedKeywordPresetId(defaultPreset.id)
        applyPresetToFilters(defaultPreset)
      } else {
        setSelectedKeywordPresetId('')
      }
    } catch {
      setKeywordPresetState({ version: 1, default_preset_id: null, presets: [] })
      setSelectedKeywordPresetId('')
    }
  }, [applyPresetToFilters])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadKeywordPresets()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [loadKeywordPresets])

  const parsedTimeline = useMemo(() => parseTimelineResponse(state.timeline), [state.timeline])
  const timelineGrid = useMemo(() => buildTimelineGrid(parsedTimeline), [parsedTimeline])
  const filterOptions = useMemo(() => buildTimelineFilterOptions(parsedTimeline), [parsedTimeline])

  useEffect(() => {
    if (!state.timeline || !parsedTimeline) {
      return
    }
    logPerf('timeline derived data', {
      eventCount: state.timeline.events.length,
      rowCount: timelineGrid.rows.length,
      laneCount: timelineGrid.laneOrder.length,
      laneOptionCount: filterOptions.laneOptions.length,
      toolOptionCount: filterOptions.toolOptions.length,
    })
  }, [filterOptions.laneOptions.length, filterOptions.toolOptions.length, parsedTimeline, state.timeline, timelineGrid.laneOrder.length, timelineGrid.rows.length])

  const selectedEvent = useMemo<TimelineEvent | null>(() => {
    if (!state.timeline || !state.selectedEventId) {
      return null
    }

    return state.timeline.events.find((event) => event.event_id === state.selectedEventId) ?? null
  }, [state.selectedEventId, state.timeline])

  const setSessionQuery = useCallback((query: string) => {
    dispatch(inspectorActions.setSessionQuery(query))
  }, [])

  const setSessionPage = useCallback((page: number) => {
    dispatch(inspectorActions.setSessionPage(page))
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

  const setFilters = useCallback((filters: Partial<TimelineFilters>) => {
    dispatch(inspectorActions.setFilters(filters))
  }, [])

  const selectEvent = useCallback((eventId: string) => {
    dispatch(inspectorActions.selectEvent(eventId))
  }, [])

  const refreshSessions = useCallback(() => {
    void loadSessions(state.sessionQuery, state.sessionPage)
  }, [loadSessions, state.sessionPage, state.sessionQuery])

  const refreshTimeline = useCallback(() => {
    if (!state.selectedSessionId) {
      return
    }
    void loadTimeline(state.selectedSessionId, state.filters)
  }, [loadTimeline, state.filters, state.selectedSessionId])

  const selectKeywordPreset = useCallback(
    (presetId: string) => {
      setSelectedKeywordPresetId(presetId)
      const preset = keywordPresetState.presets.find((item) => item.id === presetId)
      if (!preset) {
        return
      }
      applyPresetToFilters(preset)
    },
    [applyPresetToFilters, keywordPresetState.presets],
  )

  const createKeywordPreset = useCallback(
    async (name: string) => {
      const trimmedName = name.trim()
      if (!trimmedName) {
        throw new Error('预设名称不能为空')
      }
      const presetId = `preset-${Date.now().toString(36)}`
      const nextPreset: KeywordPreset = {
        id: presetId,
        name: trimmedName,
        include_keywords: state.filters.q
          .split(/[\n,，;；]/g)
          .map((item) => item.trim())
          .filter(Boolean),
        exclude_keywords: state.filters.qNot
          .split(/[\n,，;；]/g)
          .map((item) => item.trim())
          .filter(Boolean),
        updated_at: new Date().toISOString(),
      }
      const nextPayload: KeywordPresetsResponse = {
        version: 1,
        default_preset_id: presetId,
        presets: [...keywordPresetState.presets, nextPreset],
      }
      const saved = await saveKeywordPresets(nextPayload)
      setKeywordPresetState(saved)
      setSelectedKeywordPresetId(presetId)
    },
    [keywordPresetState.presets, state.filters.q, state.filters.qNot],
  )

  const updateSelectedKeywordPreset = useCallback(async () => {
    const selectedId = selectedKeywordPresetId
    if (!selectedId) {
      throw new Error('请先选择一个预设')
    }
    const current = keywordPresetState.presets.find((preset) => preset.id === selectedId)
    if (!current) {
      throw new Error('预设不存在')
    }
    const nextPreset: KeywordPreset = {
      ...current,
      include_keywords: state.filters.q
        .split(/[\n,，;；]/g)
        .map((item) => item.trim())
        .filter(Boolean),
      exclude_keywords: state.filters.qNot
        .split(/[\n,，;；]/g)
        .map((item) => item.trim())
        .filter(Boolean),
      updated_at: new Date().toISOString(),
    }
    const nextPayload: KeywordPresetsResponse = {
      version: 1,
      default_preset_id: keywordPresetState.default_preset_id ?? selectedId,
      presets: keywordPresetState.presets.map((preset) =>
        preset.id === selectedId ? nextPreset : preset,
      ),
    }
    const saved = await saveKeywordPresets(nextPayload)
    setKeywordPresetState(saved)
  }, [
    keywordPresetState.default_preset_id,
    keywordPresetState.presets,
    selectedKeywordPresetId,
    state.filters.q,
    state.filters.qNot,
  ])

  const deleteSelectedKeywordPreset = useCallback(async () => {
    const selectedId = selectedKeywordPresetId
    if (!selectedId) {
      throw new Error('请先选择一个预设')
    }
    const remain = keywordPresetState.presets.filter((preset) => preset.id !== selectedId)
    const nextDefault = remain.length > 0 ? remain[0].id : null
    const nextPayload: KeywordPresetsResponse = {
      version: 1,
      default_preset_id: nextDefault,
      presets: remain,
    }
    const saved = await saveKeywordPresets(nextPayload)
    setKeywordPresetState(saved)
    if (nextDefault) {
      setSelectedKeywordPresetId(nextDefault)
      const nextPreset = saved.presets.find((preset) => preset.id === nextDefault)
      if (nextPreset) {
        applyPresetToFilters(nextPreset)
      }
    } else {
      setSelectedKeywordPresetId('')
      setFilters({ q: '', qNot: '' })
    }
  }, [applyPresetToFilters, keywordPresetState.presets, selectedKeywordPresetId, setFilters])

  return {
    state,
    parsedTimeline,
    timelineGrid,
    filterOptions,
    selectedEvent,
    activeLaneCount: timelineGrid.laneOrder.length,
    actions: {
      setSessionQuery,
      setSessionPage,
      selectSession,
      setFilter,
      setFilters,
      selectEvent,
      refreshSessions,
      refreshTimeline,
      selectKeywordPreset,
      createKeywordPreset,
      updateSelectedKeywordPreset,
      deleteSelectedKeywordPreset,
    },
    keywordPresets: keywordPresetState.presets,
    selectedKeywordPresetId,
  }
}
