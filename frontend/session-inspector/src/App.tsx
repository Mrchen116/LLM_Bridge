import { useState } from 'react'
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

  const timelineTitle = state.selectedSessionId || '请选择 Session'
  const timelineSubtitle = state.timeline
    ? buildTimelineSubtitle(state.timeline.stats.total_events, state.timeline.stats.lane_count)
    : ''

  return (
    <div className={`app-shell ${sidePanelCollapsed ? 'side-panel-collapsed' : ''}`}>
      <SessionList
        query={state.sessionQuery}
        sessions={state.sessions}
        selectedSessionId={state.selectedSessionId}
        loading={state.sessionsLoading}
        error={state.sessionsError}
        collapsed={sidePanelCollapsed}
        onQueryChange={actions.setSessionQuery}
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

      <DetailPanel event={selectedEvent} />
    </div>
  )
}
