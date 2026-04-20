import type { SessionSummary } from '../../api/contracts'

interface SessionListProps {
  query: string
  sessions: SessionSummary[]
  selectedSessionId: string
  loading: boolean
  error: string
  collapsed: boolean
  page: number
  totalPages: number
  totalItems: number
  hasPrev: boolean
  hasNext: boolean
  onQueryChange: (value: string) => void
  onPageChange: (page: number) => void
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
  const [, , month, day, hour, minute, second] = matched
  return `${month}-${day}_${hour}-${minute}-${second}`
}

export function SessionList({
  query,
  sessions,
  selectedSessionId,
  loading,
  error,
  collapsed,
  page,
  totalPages,
  totalItems,
  hasPrev,
  hasNext,
  onQueryChange,
  onPageChange,
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

      <div className="sessions-pagination">
        <div className="sessions-pagination-summary subtle">
          第 {page} / {Math.max(1, totalPages)} 页 · {totalItems} 个 session
        </div>
        <div className="sessions-pagination-controls">
          <button
            className="btn ghost"
            type="button"
            disabled={!hasPrev}
            onClick={() => onPageChange(page - 1)}
          >
            上一页
          </button>
          <input
            className="input sessions-page-input"
            type="number"
            min={1}
            max={Math.max(1, totalPages)}
            defaultValue={page}
            key={`${page}-${totalPages}`}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') {
                return
              }
              const input = event.currentTarget
              const nextPage = Number(input.value)
              if (!Number.isFinite(nextPage) || nextPage < 1) {
                input.value = String(page)
                return
              }
              onPageChange(Math.min(nextPage, Math.max(1, totalPages)))
            }}
            onBlur={(event) => {
              const nextPage = Number(event.target.value)
              if (Number.isFinite(nextPage) && nextPage >= 1) {
                onPageChange(Math.min(nextPage, Math.max(1, totalPages)))
              } else {
                event.target.value = String(page)
              }
            }}
          />
          <button
            className="btn ghost"
            type="button"
            disabled={!hasNext}
            onClick={() => onPageChange(page + 1)}
          >
            下一页
          </button>
        </div>
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
