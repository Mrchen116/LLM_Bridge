import { useSessionInspectorController } from './business/use-session-inspector-controller'
import { SessionList } from './components/session-list/SessionList'
import { TimelineLanes } from './components/timeline-lanes/TimelineLanes'
import { DetailPanel } from './components/detail-panel/DetailPanel'

function buildTimelineSubtitle(totalEvents: number, laneCount: number): string {
  return `${totalEvents} events · ${laneCount} lanes`
}

export default function App() {
  const { state, timelineGrid, filterOptions, selectedEvent, actions } = useSessionInspectorController()

  const timelineTitle = state.selectedSessionId || '请选择 Session'
  const timelineSubtitle = state.timeline
    ? buildTimelineSubtitle(state.timeline.stats.total_events, state.timeline.stats.lane_count)
    : ''

  return (
    <div className="app-shell">
      <SessionList
        query={state.sessionQuery}
        sessions={state.sessions}
        selectedSessionId={state.selectedSessionId}
        loading={state.sessionsLoading}
        error={state.sessionsError}
        onQueryChange={actions.setSessionQuery}
        onRefresh={actions.refreshSessions}
        onSelectSession={actions.selectSession}
      />

      <TimelineLanes
        title={timelineTitle}
        subtitle={timelineSubtitle}
        warnings={state.timeline?.meta.warnings ?? []}
        loading={state.timelineLoading}
        error={state.timelineError}
        filters={state.filters}
        laneOptions={filterOptions.laneOptions}
        toolOptions={filterOptions.toolOptions}
        grid={timelineGrid}
        selectedEventId={state.selectedEventId}
        onRefresh={actions.refreshTimeline}
        onFilterChange={{
          agent: (value) => actions.setFilter('agent', value),
          tool: (value) => actions.setFilter('tool', value),
          q: (value) => actions.setFilter('q', value),
          includeNonTool: (value) => actions.setFilter('includeNonTool', value),
        }}
        onSelectEvent={actions.selectEvent}
      />

      <DetailPanel event={selectedEvent} />
    </div>
  )
}
