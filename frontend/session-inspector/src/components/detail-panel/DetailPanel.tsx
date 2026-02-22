import { useEffect, useState } from 'react'
import type { TimelineEvent } from '../../api/contracts'

interface DetailPanelProps {
  event: TimelineEvent | null
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function DetailBlock({
  title,
  value,
  copyLabel,
  copied,
  onCopy,
}: {
  title: string
  value: unknown
  copyLabel: string
  copied: boolean
  onCopy: (text: string) => void
}) {
  const text = formatValue(value)

  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">{title}</div>
        <button className="code-copy" type="button" onClick={() => onCopy(text)}>
          {copied ? '已复制' : copyLabel}
        </button>
      </div>
      <pre className="code">{text}</pre>
    </div>
  )
}

export function DetailPanel({ event }: DetailPanelProps) {
  const [copiedKey, setCopiedKey] = useState('')

  useEffect(() => {
    if (!copiedKey) {
      return
    }

    const timer = window.setTimeout(() => setCopiedKey(''), 1200)
    return () => window.clearTimeout(timer)
  }, [copiedKey])

  const onCopy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
    } catch {
      setCopiedKey('')
    }
  }

  return (
    <section className="panel detail-panel">
      <header className="panel-header">
        <div className="panel-title-group">
          <h2>事件详情</h2>
          <p className="subtle">展示结构化事件信息</p>
        </div>
      </header>

      <div className="detail-body">
        {!event ? <div className="subtle">点击事件查看详情</div> : null}

        {event ? (
          <>
            <DetailBlock
              title="事件"
              value={`${event.kind} · ${event.ts}`}
              copyLabel="复制"
              copied={copiedKey === 'kind'}
              onCopy={(text) => void onCopy('kind', text)}
            />
            <DetailBlock
              title="摘要"
              value={event.summary || ''}
              copyLabel="复制"
              copied={copiedKey === 'summary'}
              onCopy={(text) => void onCopy('summary', text)}
            />

            {event.kind === 'tool_call' ? (
              <>
                <DetailBlock
                  title="工具名"
                  value={event.tool_name || ''}
                  copyLabel="复制"
                  copied={copiedKey === 'tool_name'}
                  onCopy={(text) => void onCopy('tool_name', text)}
                />
                <DetailBlock
                  title="工具参数"
                  value={event.tool_args ?? {}}
                  copyLabel="复制"
                  copied={copiedKey === 'tool_args'}
                  onCopy={(text) => void onCopy('tool_args', text)}
                />
                {event.tool_def ? (
                  <DetailBlock
                    title="工具定义"
                    value={event.tool_def}
                    copyLabel="复制"
                    copied={copiedKey === 'tool_def'}
                    onCopy={(text) => void onCopy('tool_def', text)}
                  />
                ) : null}
              </>
            ) : null}

            <DetailBlock
              title="完整事件"
              value={event}
              copyLabel="复制"
              copied={copiedKey === 'full'}
              onCopy={(text) => void onCopy('full', text)}
            />
          </>
        ) : null}
      </div>
    </section>
  )
}
