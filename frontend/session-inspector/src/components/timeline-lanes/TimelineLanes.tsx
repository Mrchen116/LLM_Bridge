import type { KeywordPreset } from '../../api/contracts'
import type { TimelineFilters } from '../../state/types'
import type { SelectOption, TimelineGrid } from '../../lib/timeline-cluster'
import { EventCard } from '../event-card/EventCard'

interface TimelineLanesProps {
  title: string
  subtitle: string
  warnings: string[]
  loading: boolean
  error: string
  filters: TimelineFilters
  laneOptions: SelectOption[]
  toolOptions: SelectOption[]
  keywordPresets: KeywordPreset[]
  selectedKeywordPresetId: string
  grid: TimelineGrid
  selectedEventId: string
  onRefresh: () => void
  onFilterChange: {
    agent: (value: string) => void
    tool: (value: string) => void
    q: (value: string) => void
    qNot: (value: string) => void
    includeNonTool: (value: boolean) => void
  }
  onSelectKeywordPreset: (presetId: string) => void
  onCreateKeywordPreset: (name: string) => Promise<void>
  onUpdateKeywordPreset: () => Promise<void>
  onDeleteKeywordPreset: () => Promise<void>
  onSelectEvent: (eventId: string) => void
}

function buildSwimlaneTemplate(laneCount: number): string {
  const timeColumn = 88
  const laneMinWidth = 210
  return `${timeColumn}px repeat(${laneCount}, minmax(${laneMinWidth}px, 1fr))`
}

export function TimelineLanes({
  title,
  subtitle,
  warnings,
  loading,
  error,
  filters,
  laneOptions,
  toolOptions,
  keywordPresets,
  selectedKeywordPresetId,
  grid,
  selectedEventId,
  onRefresh,
  onFilterChange,
  onSelectKeywordPreset,
  onCreateKeywordPreset,
  onUpdateKeywordPreset,
  onDeleteKeywordPreset,
  onSelectEvent,
}: TimelineLanesProps) {
  async function handleCreatePreset() {
    const name = window.prompt('输入新预设名称')
    if (!name) {
      return
    }
    try {
      await onCreateKeywordPreset(name)
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  async function handleUpdatePreset() {
    try {
      await onUpdateKeywordPreset()
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  async function handleDeletePreset() {
    const ok = window.confirm('确认删除当前关键词预设吗？')
    if (!ok) {
      return
    }
    try {
      await onDeleteKeywordPreset()
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  return (
    <main className="panel timeline-panel timeline-panel-dense">
      <header className="panel-header">
        <div className="panel-title-group">
          <h2>{title}</h2>
          <p className="subtle">{subtitle}</p>
        </div>
        <div className="panel-actions">
          <button className="btn" type="button" onClick={onRefresh}>
            刷新时间线
          </button>
        </div>
      </header>

      <div className="filters">
        <div className="filters-row filters-row-main">
          <select
            className="input"
            value={filters.agent}
            onChange={(event) => onFilterChange.agent(event.target.value)}
          >
            {laneOptions.map((option) => (
              <option key={`lane-${option.value}`} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          <select
            className="input"
            value={filters.tool}
            onChange={(event) => onFilterChange.tool(event.target.value)}
          >
            {toolOptions.map((option) => (
              <option key={`tool-${option.value}`} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          <input
            className="input"
            type="text"
            value={filters.q}
            placeholder="正向关键词（逗号/换行分隔）"
            onChange={(event) => onFilterChange.q(event.target.value)}
          />

          <input
            className="input"
            type="text"
            value={filters.qNot}
            placeholder="反向关键词（命中整轮剔除）"
            onChange={(event) => onFilterChange.qNot(event.target.value)}
          />
        </div>

        <div className="filters-row filters-row-extra">
          <select
            className="input"
            value={selectedKeywordPresetId}
            onChange={(event) => onSelectKeywordPreset(event.target.value)}
          >
            <option value="">关键词预设</option>
            {keywordPresets.map((preset) => (
              <option key={`keyword-preset-${preset.id}`} value={preset.id}>
                {preset.name}
              </option>
            ))}
          </select>

          <div className="filters-actions">
            <button className="btn ghost" type="button" onClick={handleCreatePreset}>
              保存新预设
            </button>
            <button className="btn ghost" type="button" onClick={handleUpdatePreset}>
              更新预设
            </button>
            <button className="btn ghost" type="button" onClick={handleDeletePreset}>
              删除预设
            </button>
          </div>

          <label className="check-line">
            <input
              type="checkbox"
              checked={filters.includeNonTool}
              onChange={(event) => onFilterChange.includeNonTool(event.target.checked)}
            />
            <span>显示非工具事件</span>
          </label>
        </div>
      </div>

      <section className="panel-scroll timeline-scroll">
        {loading ? <div className="subtle">加载中...</div> : null}
        {!loading && error ? <div className="subtle">加载失败：{error}</div> : null}
        {!loading && !error && warnings.length > 0 ? (
          <div className="subtle warning-text">{warnings.join('；')}</div>
        ) : null}
        {!loading && !error && grid.laneOrder.length === 0 ? (
          <div className="subtle">当前过滤条件下没有事件</div>
        ) : null}

        {!loading && !error && grid.laneOrder.length > 0 ? (
          <div className="swimlane-board">
            <div
              className="swimlane-header"
              style={{ gridTemplateColumns: buildSwimlaneTemplate(grid.laneOrder.length) }}
            >
              <div className="swimlane-time-head">时间</div>
              {grid.laneOrder.map((lane) => (
                <div className="swimlane-lane-head" key={lane.lane_id}>
                  <span className="lane-label" title={lane.label}>
                    {lane.label}
                  </span>
                  <span className="lane-count">{lane.event_count}</span>
                </div>
              ))}
            </div>

            <div className="swimlane-body">
              {grid.rows.map((row) => (
                <div
                  className="swimlane-row"
                  key={`row-${row.eventId}`}
                  style={{ gridTemplateColumns: buildSwimlaneTemplate(grid.laneOrder.length) }}
                >
                  <div className="swimlane-time-cell">{row.timestampLabel}</div>
                  {row.cells.map((cell) => (
                    <div
                      className={`swimlane-cell ${cell.event ? '' : 'empty'}`}
                      key={`cell-${row.eventId}-${cell.laneId}`}
                    >
                      {cell.event ? (
                        <EventCard
                          event={cell.event}
                          selected={selectedEventId === cell.event.eventId}
                          onSelect={onSelectEvent}
                        />
                      ) : null}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>
    </main>
  )
}
