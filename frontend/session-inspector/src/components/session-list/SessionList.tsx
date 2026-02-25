import type { SessionSummary } from '../../api/contracts'

interface SessionListProps {
  query: string
  sessions: SessionSummary[]
  selectedSessionId: string
  loading: boolean
  error: string
  collapsed: boolean
  onQueryChange: (value: string) => void
  onRefresh: () => void
  onSelectSession: (sessionId: string, sessionDir: string) => void
  onToggleCollapse: () => void
}

function formatSessionStartTag(startTs: string): string {
  const normalized = (startTs || '').trim()
  const matched = normalized.match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/)
  if (!matched) {
    return normalized
  }
  const [, _year, month, day, hour, minute, second] = matched
  return `${month}-${day}_${hour}-${minute}-${second}`
}

export function SessionList({
  query,
  sessions,
  selectedSessionId,
  loading,
  error,
  collapsed,
  onQueryChange,
  onRefresh,
  onSelectSession,
  onToggleCollapse,
}: SessionListProps) {
  if (collapsed) {
    return (
      <aside className="sessions-collapsed-shell" aria-label="会话列表已折叠">
        <button
          className="sessions-collapsed-handle"
          type="button"
          title="展开会话列表"
          aria-label="展开会话列表"
          onClick={onToggleCollapse}
        >
          <span className="sessions-collapsed-arrow" aria-hidden="true">
            &gt;
          </span>
        </button>
      </aside>
    )
  }

  return (
    <aside className="panel sessions-panel">
      <header className="panel-header">
        <div className="panel-title-group">
          <h1>Session Inspector</h1>
          <p className="subtle">会话总览</p>
        </div>
        <div className="panel-actions">
          <button
            className="btn ghost icon-btn"
            type="button"
            title="折叠会话列表"
            aria-label="折叠会话列表"
            onClick={onToggleCollapse}
          >
            &lt;
          </button>
          <button className="btn ghost" type="button" onClick={onRefresh}>
            刷新
          </button>
        </div>
      </header>

      <div className="sessions-controls">
        <input
          className="input"
          type="text"
          value={query}
          placeholder="搜索 session_id"
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </div>

      <div className="panel-scroll sessions-list">
        {loading ? <div className="subtle">加载中...</div> : null}
        {!loading && error ? <div className="subtle">加载失败：{error}</div> : null}
        {!loading && !error && sessions.length === 0 ? (
          <div className="subtle">暂无会话</div>
        ) : null}

        {sessions.map((session) => (
          <button
            key={session.session_id}
            type="button"
            className={`session-item ${selectedSessionId === session.session_id ? 'active' : ''}`}
            onClick={() => onSelectSession(session.session_id, session.session_dir)}
          >
            <div className="session-id">{session.session_id}</div>
            <div className="session-meta">
              {session.turn_count} turns · {session.formats.join(', ') || 'unknown'} ·{' '}
              {formatSessionStartTag(session.start_ts)}
            </div>
          </button>
        ))}
      </div>
    </aside>
  )
}
