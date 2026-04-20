import { describe, expect, it } from 'vitest'
import { inspectorActions } from '../actions'
import { inspectorReducer } from '../reducer'
import { initialInspectorState } from '../types'

describe('inspectorReducer', () => {
  it('updates session query', () => {
    const next = inspectorReducer(initialInspectorState, inspectorActions.setSessionQuery('demo'))
    expect(next.sessionQuery).toBe('demo')
  })

  it('loads sessions payload', () => {
    const loading = inspectorReducer(initialInspectorState, inspectorActions.sessionsLoading())
    expect(loading.sessionsLoading).toBe(true)

    const loaded = inspectorReducer(
      loading,
      inspectorActions.sessionsLoaded(
        [
          {
            session_id: 'demo-session',
            session_dir: '2026-demo-session',
            start_ts: '2026-02-22_12-00-00_000',
            end_ts: '2026-02-22_12-00-10_000',
            turn_count: 3,
            formats: ['openai_chat'],
          },
        ],
        2,
        50,
        120,
        3,
        true,
        false,
      ),
    )

    expect(loaded.sessionsLoading).toBe(false)
    expect(loaded.sessions[0].session_id).toBe('demo-session')
    expect(loaded.sessionPage).toBe(2)
    expect(loaded.sessionsTotalItems).toBe(120)
  })

  it('resets session page when query changes', () => {
    const seeded = {
      ...initialInspectorState,
      sessionPage: 4,
    }

    const next = inspectorReducer(seeded, inspectorActions.setSessionQuery('demo'))
    expect(next.sessionQuery).toBe('demo')
    expect(next.sessionPage).toBe(1)
  })

  it('resets selected event when filter changes', () => {
    const seeded = {
      ...initialInspectorState,
      selectedEventId: 'event-1',
    }

    const next = inspectorReducer(seeded, inspectorActions.setFilter('q', 'bash'))
    expect(next.filters.q).toBe('bash')
    expect(next.selectedEventId).toBe('')
  })
})
