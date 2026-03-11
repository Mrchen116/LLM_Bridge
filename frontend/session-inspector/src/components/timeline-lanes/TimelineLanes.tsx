import { useMemo, useState } from 'react'
import type { KeywordPreset, TimelineResponse } from '../../api/contracts'
import type { TimelineFilters } from '../../state/types'
import type { SelectOption, TimelineGrid } from '../../lib/timeline-cluster'
import { EventCard } from '../event-card/EventCard'

interface TimelineLanesProps {
  title: string
  subtitle: string
  stats: TimelineResponse['stats'] | null
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
  stats,
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
  const [statsModalOpen, setStatsModalOpen] = useState(false)
  const [selectedStatsLaneId, setSelectedStatsLaneId] = useState('')

  const filteredScopeStats = stats?.filtered_scope ?? null
  const selectedAgentStats = useMemo(() => {
    if (!filteredScopeStats || filteredScopeStats.agents.length === 0) {
      return null
    }
    if (!selectedStatsLaneId) {
      return filteredScopeStats.agents[0]
    }
    return (
      filteredScopeStats.agents.find((item) => item.lane_id === selectedStatsLaneId) ??
      filteredScopeStats.agents[0]
    )
  }, [filteredScopeStats, selectedStatsLaneId])

  function formatCount(value: number): string {
    return value.toLocaleString()
  }

  function formatDuration(durationMs: number): string {
    if (durationMs < 1000) {
      return `${durationMs} ms`
    }
    if (durationMs < 60_000) {
      return `${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)} s`
    }

    const totalSeconds = Math.floor(durationMs / 1000)
    const minutes = Math.floor(totalSeconds / 60)
    const seconds = totalSeconds % 60
    return `${minutes}m ${seconds}s`
  }

  function openStatsModal() {
    if (!filteredScopeStats) {
      return
    }
    if (!selectedStatsLaneId && filteredScopeStats.agents.length > 0) {
      setSelectedStatsLaneId(filteredScopeStats.agents[0].lane_id)
    }
    setStatsModalOpen(true)
  }

  function openStatsModalForLane(laneId: string) {
    if (!filteredScopeStats) {
      return
    }
    setSelectedStatsLaneId(laneId)
    setStatsModalOpen(true)
  }

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

  const statsCards = filteredScopeStats
    ? [
        {
          label: 'Input Tokens',
          value: formatCount(filteredScopeStats.session_tokens.input_tokens),
          help: '关键词过滤后的 turns 中，所有请求消耗的输入 token 总和，来自日志里的 usage 字段聚合。',
        },
        {
          label: 'Output Tokens',
          value: formatCount(filteredScopeStats.session_tokens.output_tokens),
          help: '关键词过滤后的 turns 中，所有回复消耗的输出 token 总和，来自日志里的 usage 字段聚合。',
        },
        {
          label: 'Turns',
          value: formatCount(filteredScopeStats.session_tokens.num_turns),
          help: '关键词过滤后被保留下来的 turn 数量；一个 turn 对应一次请求及其最终回复。',
        },
        {
          label: 'Tool Calls',
          value: formatCount(filteredScopeStats.tool_calls.total_calls),
          help: '关键词过滤后的 turns 中，识别到的 tool_call 事件总数。',
        },
        {
          label: 'Duration',
          value: formatDuration(filteredScopeStats.duration.duration_ms),
          help: '关键词过滤后的 turns 中，从第一个请求发起时间到最后一个回复获取时间的时间差。',
        },
      ]
    : []

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
        {filteredScopeStats ? (
          <div className="stats-summary-strip">
            <div className="stats-chip">
              Session Tokens: in {formatCount(filteredScopeStats.session_tokens.input_tokens)} / out{' '}
              {formatCount(filteredScopeStats.session_tokens.output_tokens)}
            </div>
            <div className="stats-chip">
              Turns {formatCount(filteredScopeStats.session_tokens.num_turns)} · Tool Calls{' '}
              {formatCount(filteredScopeStats.tool_calls.total_calls)}
            </div>
            <div className="stats-chip">Duration {formatDuration(filteredScopeStats.duration.duration_ms)}</div>
            <div className="stats-chip">
              Agents {formatCount(filteredScopeStats.agents.length)} · Keyword Turns{' '}
              {formatCount(filteredScopeStats.turn_count_after_keywords)}
            </div>
            <button className="btn ghost stats-open-btn" type="button" onClick={openStatsModal}>
              查看统计
            </button>
          </div>
        ) : null}

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
                <button
                  type="button"
                  className="swimlane-lane-head lane-head-btn"
                  key={lane.lane_id}
                  onClick={() => openStatsModalForLane(lane.lane_id)}
                  title="点击查看该 Agent 统计"
                >
                  <span className="lane-label" title={lane.label}>
                    {lane.label}
                  </span>
                  <span className="lane-count">{lane.event_count}</span>
                </button>
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

      {statsModalOpen && filteredScopeStats ? (
        <div className="stats-modal-backdrop" onClick={() => setStatsModalOpen(false)}>
          <div className="stats-modal" onClick={(event) => event.stopPropagation()}>
            <div className="stats-modal-head">
              <div>
                <div className="stats-modal-title">Session 统计</div>
                <div className="subtle">统计范围：仅关键词过滤后的 turns</div>
              </div>
              <button className="btn ghost" type="button" onClick={() => setStatsModalOpen(false)}>
                关闭
              </button>
            </div>

            <div className="stats-kv-grid">
              {statsCards.map((item) => (
                <div className="stats-kv-card" key={`stats-card-${item.label}`}>
                  <div className="stats-kv-head">
                    <div className="stats-kv-label">{item.label}</div>
                    <span
                      className="stats-kv-help"
                      data-help={item.help}
                      aria-label={`${item.label} 说明：${item.help}`}
                      tabIndex={0}
                    >
                      ?
                    </span>
                  </div>
                  <div className="stats-kv-value">{item.value}</div>
                </div>
              ))}
            </div>

            <div className="stats-modal-body">
              <section className="stats-panel">
                <div className="stats-panel-title">工具调用分布</div>
                <div className="stats-table-wrap">
                  {filteredScopeStats.tool_calls.by_tool.length > 0 ? (
                    <table className="stats-table">
                      <thead>
                        <tr>
                          <th>Tool</th>
                          <th>Count</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredScopeStats.tool_calls.by_tool.map((item) => (
                          <tr key={`session-tool-${item.tool_name}`}>
                            <td title={item.tool_name}>{item.tool_name}</td>
                            <td>{formatCount(item.count)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <div className="subtle">当前关键词范围下没有工具调用</div>
                  )}
                </div>
              </section>

              <section className="stats-panel">
                <div className="stats-panel-title">Agent 统计</div>
                <div className="stats-agent-layout">
                  <div className="stats-agent-list">
                    {filteredScopeStats.agents.map((agentStat) => {
                      const active =
                        (selectedAgentStats?.lane_id ?? filteredScopeStats.agents[0].lane_id) ===
                        agentStat.lane_id
                      return (
                        <button
                          key={`agent-stats-${agentStat.lane_id}`}
                          type="button"
                          className={`stats-agent-item ${active ? 'active' : ''}`}
                          onClick={() => setSelectedStatsLaneId(agentStat.lane_id)}
                        >
                          <div className="stats-agent-label" title={agentStat.label}>
                            {agentStat.label}
                          </div>
                          <div className="stats-agent-meta">
                            turns {formatCount(agentStat.tokens.num_turns)} · tools{' '}
                            {formatCount(agentStat.tool_calls_total)} · time{' '}
                            {formatDuration(agentStat.duration.duration_ms)}
                          </div>
                        </button>
                      )
                    })}
                  </div>

                  <div className="stats-agent-detail">
                    {selectedAgentStats ? (
                      <>
                        <div className="stats-agent-detail-title" title={selectedAgentStats.label}>
                          {selectedAgentStats.label}
                        </div>
                        <div className="stats-agent-detail-kv">
                          <div>Input: {formatCount(selectedAgentStats.tokens.input_tokens)}</div>
                          <div>Output: {formatCount(selectedAgentStats.tokens.output_tokens)}</div>
                          <div>Turns: {formatCount(selectedAgentStats.tokens.num_turns)}</div>
                          <div>Duration: {formatDuration(selectedAgentStats.duration.duration_ms)}</div>
                          <div>Tool Calls: {formatCount(selectedAgentStats.tool_calls_total)}</div>
                        </div>
                        <div className="stats-table-wrap">
                          {selectedAgentStats.tool_calls_by_name.length > 0 ? (
                            <table className="stats-table">
                              <thead>
                                <tr>
                                  <th>Tool</th>
                                  <th>Count</th>
                                </tr>
                              </thead>
                              <tbody>
                                {selectedAgentStats.tool_calls_by_name.map((item) => (
                                  <tr key={`agent-tool-${selectedAgentStats.lane_id}-${item.tool_name}`}>
                                    <td title={item.tool_name}>{item.tool_name}</td>
                                    <td>{formatCount(item.count)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          ) : (
                            <div className="subtle">该 Agent 在关键词范围下没有工具调用</div>
                          )}
                        </div>
                      </>
                    ) : (
                      <div className="subtle">当前关键词范围下没有 Agent 统计数据</div>
                    )}
                  </div>
                </div>
              </section>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  )
}
