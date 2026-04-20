import { useCallback, useEffect, useRef, useState } from 'react'
import { useSessionInspectorController } from './business/use-session-inspector-controller'
import { SessionList } from './components/session-list/SessionList'
import { TimelineLanes } from './components/timeline-lanes/TimelineLanes'
import { DetailPanel } from './components/detail-panel/DetailPanel'

function buildTimelineSubtitle(totalEvents: number, laneCount: number): string {
  return `${totalEvents} events · ${laneCount} lanes`
}

export default function App() {
  const {
    state,
    timelineGrid,
    filterOptions,
    selectedEvent,
    actions,
    keywordPresets,
    selectedKeywordPresetId,
  } = useSessionInspectorController()
  const [sidePanelCollapsed, setSidePanelCollapsed] = useState(false)
  const [rightPanelWidth, setRightPanelWidth] = useState(320)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  const onResizeStart = useCallback((startX: number) => {
    dragRef.current = { startX, startWidth: rightPanelWidth }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }, [rightPanelWidth])

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragRef.current) return
      const delta = dragRef.current.startX - e.clientX
      const next = Math.min(700, Math.max(240, dragRef.current.startWidth + delta))
      setRightPanelWidth(next)
    }
    const onMouseUp = () => {
      if (!dragRef.current) return
      dragRef.current = null
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [])

  const timelineTitle = state.selectedSessionId || '请选择 Session'
  const timelineSubtitle = state.timeline
    ? buildTimelineSubtitle(state.timeline.stats.total_events, state.timeline.stats.lane_count)
    : ''

  return (
    <div
      className={`app-shell ${sidePanelCollapsed ? 'side-panel-collapsed' : ''}`}
      style={{ '--right-panel-width': `${rightPanelWidth}px` } as React.CSSProperties}
    >
      <SessionList
        query={state.sessionQuery}
        sessions={state.sessions}
        selectedSessionId={state.selectedSessionId}
        loading={state.sessionsLoading}
        error={state.sessionsError}
        collapsed={sidePanelCollapsed}
        page={state.sessionPage}
        totalPages={state.sessionsTotalPages}
        totalItems={state.sessionsTotalItems}
        hasPrev={state.sessionsHasPrev}
        hasNext={state.sessionsHasNext}
        onQueryChange={actions.setSessionQuery}
        onPageChange={actions.setSessionPage}
        onRefresh={actions.refreshSessions}
        onSelectSession={actions.selectSession}
        onToggleCollapse={() => setSidePanelCollapsed((previous) => !previous)}
      />

      <TimelineLanes
        title={timelineTitle}
        subtitle={timelineSubtitle}
        stats={state.timeline?.stats ?? null}
        warnings={state.timeline?.meta.warnings ?? []}
        loading={state.timelineLoading}
        error={state.timelineError}
        filters={state.filters}
        laneOptions={filterOptions.laneOptions}
        toolOptions={filterOptions.toolOptions}
        keywordPresets={keywordPresets}
        selectedKeywordPresetId={selectedKeywordPresetId}
        grid={timelineGrid}
        selectedEventId={state.selectedEventId}
        onRefresh={actions.refreshTimeline}
        onFilterChange={{
          agent: (value) => actions.setFilter('agent', value),
          tool: (value) => actions.setFilter('tool', value),
          q: (value) => actions.setFilter('q', value),
          qNot: (value) => actions.setFilter('qNot', value),
          includeNonTool: (value) => actions.setFilter('includeNonTool', value),
        }}
        onSelectKeywordPreset={actions.selectKeywordPreset}
        onCreateKeywordPreset={actions.createKeywordPreset}
        onUpdateKeywordPreset={actions.updateSelectedKeywordPreset}
        onDeleteKeywordPreset={actions.deleteSelectedKeywordPreset}
        onSelectEvent={actions.selectEvent}
      />

      <DetailPanel event={selectedEvent} sessionId={state.selectedSessionId} onResizeStart={onResizeStart} />
    </div>
  )
}
