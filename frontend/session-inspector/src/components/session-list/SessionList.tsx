import type { SessionSummary } from '../../api/contracts'

interface SessionListProps {
  query: string
  sessions: SessionSummary[]
  selectedSessionId: string
  loading: boolean
  error: string
  onQueryChange: (value: string) => void
  onRefresh: () => void
  onSelectSession: (sessionId: string, sessionDir: string) => void
}

export function SessionList({
  query,
  sessions,
  selectedSessionId,
  loading,
  error,
  onQueryChange,
  onRefresh,
  onSelectSession,
}: SessionListProps) {
  return (
    <aside className="panel sessions-panel">
      <header className="panel-header">
        <div className="panel-title-group">
          <h1>Session Inspector</h1>
          <p className="subtle">会话总览</p>
        </div>
        <button className="btn ghost" type="button" onClick={onRefresh}>
          刷新
        </button>
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
              {session.turn_count} turns · {session.formats.join(', ') || 'unknown'}
            </div>
          </button>
        ))}
      </div>
    </aside>
  )
}
