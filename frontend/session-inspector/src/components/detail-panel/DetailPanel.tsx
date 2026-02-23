import { useEffect, useMemo, useState } from 'react'
import type { TimelineEvent, ToolDefinition } from '../../api/contracts'
import { fetchLogFileContent } from '../../api/session-inspector-client'
import {
  extractEventMainText,
  formatCodeValue,
  normalizeReadableText,
} from '../../lib/event-display'

interface DetailPanelProps {
  event: TimelineEvent | null
}

type BlockVariant = 'text' | 'code'

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function formatCopyValue(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatDisplayValue(value: unknown, variant: BlockVariant): string {
  if (variant === 'text') {
    return normalizeReadableText(typeof value === 'string' ? value : formatCopyValue(value))
  }
  return formatCodeValue(value)
}

function DetailBlock({
  title,
  value,
  variant,
  copyLabel,
  copied,
  onCopy,
}: {
  title: string
  value: unknown
  variant: BlockVariant
  copyLabel: string
  copied: boolean
  onCopy: (text: string) => void
}) {
  const copyText = formatCopyValue(value)
  const displayText = formatDisplayValue(value, variant)

  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">{title}</div>
        <button className="code-copy" type="button" onClick={() => onCopy(copyText)}>
          {copied ? '已复制' : copyLabel}
        </button>
      </div>
      <pre className={variant === 'text' ? 'detail-text' : 'code'}>{displayText}</pre>
    </div>
  )
}

function SourceFilesBlock({
  sourceFiles,
  copied,
  onCopy,
  onView,
}: {
  sourceFiles: TimelineEvent['source_files']
  copied: boolean
  onCopy: (text: string) => void
  onView: (path: string) => void
}) {
  const rows = [
    { key: 'request', label: '请求' },
    { key: 'response', label: '响应' },
    { key: 'non_stream_response', label: '非流式响应' },
    { key: 'downstream_response', label: '下游响应' },
  ] as const

  const availableRows = rows.filter((row) => {
    const value = sourceFiles?.[row.key]
    return typeof value === 'string' && value.trim().length > 0
  })
  const copyPayload = availableRows
    .map((row) => `${row.key}: ${String(sourceFiles?.[row.key] ?? '')}`)
    .join('\n')

  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">日志文件</div>
        <button className="code-copy" type="button" onClick={() => onCopy(copyPayload)}>
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      {availableRows.length > 0 ? (
        <div className="source-files">
          {availableRows.map((row) => (
            <div className="source-file-row" key={`source-${row.key}`}>
              <span className="source-file-label">{row.label}</span>
              <div className="source-file-main">
                <code className="source-file-path">{String(sourceFiles?.[row.key] ?? '')}</code>
                <button
                  className="link-btn"
                  type="button"
                  onClick={() => onView(String(sourceFiles?.[row.key] ?? ''))}
                >
                  查看
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="subtle">无日志文件路径</div>
      )}
    </div>
  )
}

function extractToolDefinitionFields(toolDef: ToolDefinition | null | undefined): {
  name: string
  description: string
  parameters: unknown
} {
  if (!toolDef || !isObjectRecord(toolDef)) {
    return {
      name: '',
      description: '',
      parameters: {},
    }
  }

  const name = typeof toolDef.name === 'string' ? toolDef.name : ''
  const description =
    typeof toolDef.description === 'string' ? normalizeReadableText(toolDef.description) : ''
  const parameters = toolDef.parameters ?? {}

  return {
    name,
    description,
    parameters,
  }
}

export function DetailPanel({ event }: DetailPanelProps) {
  const [copiedKey, setCopiedKey] = useState('')
  const [viewerOpen, setViewerOpen] = useState(false)
  const [viewerPath, setViewerPath] = useState('')
  const [viewerContent, setViewerContent] = useState('')
  const [viewerTruncated, setViewerTruncated] = useState(false)
  const [viewerLoading, setViewerLoading] = useState(false)
  const [viewerError, setViewerError] = useState('')
  const mainText = useMemo(() => (event ? extractEventMainText(event) : ''), [event])
  const toolDefFields = useMemo(() => extractToolDefinitionFields(event?.tool_def), [event])

  useEffect(() => {
    if (!copiedKey) {
      return
    }

    const timer = window.setTimeout(() => setCopiedKey(''), 1200)
    return () => window.clearTimeout(timer)
  }, [copiedKey])

  useEffect(() => {
    if (!viewerOpen) {
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setViewerOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [viewerOpen])

  const onCopy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
    } catch {
      setCopiedKey('')
    }
  }

  const onViewSourceFile = async (path: string) => {
    const normalizedPath = path.trim()
    if (!normalizedPath) {
      return
    }
    setViewerOpen(true)
    setViewerPath(normalizedPath)
    setViewerLoading(true)
    setViewerError('')
    setViewerContent('')
    setViewerTruncated(false)
    try {
      const payload = await fetchLogFileContent(normalizedPath)
      setViewerPath(payload.path)
      setViewerContent(payload.content)
      setViewerTruncated(payload.truncated)
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error))
      setViewerContent('')
    } finally {
      setViewerLoading(false)
    }
  }

  return (
    <>
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
                variant="text"
                copyLabel="复制"
                copied={copiedKey === 'kind'}
                onCopy={(text) => void onCopy('kind', text)}
              />
              <DetailBlock
                title="摘要"
                value={event.summary || ''}
                variant="text"
                copyLabel="复制"
                copied={copiedKey === 'summary'}
                onCopy={(text) => void onCopy('summary', text)}
              />
              <DetailBlock
                title="事件内容"
                value={mainText}
                variant="text"
                copyLabel="复制"
                copied={copiedKey === 'main_text'}
                onCopy={(text) => void onCopy('main_text', text)}
              />
              <SourceFilesBlock
                sourceFiles={event.source_files}
                copied={copiedKey === 'source_files'}
                onCopy={(text) => void onCopy('source_files', text)}
                onView={onViewSourceFile}
              />

              {event.kind === 'tool_call' ? (
                <>
                  <DetailBlock
                    title="工具名"
                    value={event.tool_name || ''}
                    variant="text"
                    copyLabel="复制"
                    copied={copiedKey === 'tool_name'}
                    onCopy={(text) => void onCopy('tool_name', text)}
                  />
                  <DetailBlock
                    title="工具参数"
                    value={event.tool_args ?? {}}
                    variant="code"
                    copyLabel="复制"
                    copied={copiedKey === 'tool_args'}
                    onCopy={(text) => void onCopy('tool_args', text)}
                  />
                  {event.tool_def ? (
                    <>
                      <DetailBlock
                        title="工具定义 · 名称"
                        value={toolDefFields.name}
                        variant="text"
                        copyLabel="复制"
                        copied={copiedKey === 'tool_def_name'}
                        onCopy={(text) => void onCopy('tool_def_name', text)}
                      />
                      <DetailBlock
                        title="工具定义 · 描述"
                        value={toolDefFields.description}
                        variant="text"
                        copyLabel="复制"
                        copied={copiedKey === 'tool_def_description'}
                        onCopy={(text) => void onCopy('tool_def_description', text)}
                      />
                      <DetailBlock
                        title="工具定义 · 参数"
                        value={toolDefFields.parameters}
                        variant="code"
                        copyLabel="复制"
                        copied={copiedKey === 'tool_def_parameters'}
                        onCopy={(text) => void onCopy('tool_def_parameters', text)}
                      />
                    </>
                  ) : null}
                </>
              ) : null}

              <details className="raw-event">
                <summary>完整事件 JSON</summary>
                <DetailBlock
                  title="完整事件"
                  value={event}
                  variant="code"
                  copyLabel="复制"
                  copied={copiedKey === 'full'}
                  onCopy={(text) => void onCopy('full', text)}
                />
              </details>
            </>
          ) : null}
        </div>
      </section>

      {viewerOpen ? (
        <div className="file-viewer-backdrop" onClick={() => setViewerOpen(false)}>
          <div className="file-viewer-modal" onClick={(event) => event.stopPropagation()}>
            <div className="file-viewer-head">
              <div className="file-viewer-title">日志文件内容</div>
              <button className="btn ghost" type="button" onClick={() => setViewerOpen(false)}>
                关闭
              </button>
            </div>
            <div className="file-viewer-path">{viewerPath}</div>
            {viewerLoading ? <div className="subtle">加载中...</div> : null}
            {!viewerLoading && viewerError ? (
              <div className="subtle">读取失败：{viewerError}</div>
            ) : null}
            {!viewerLoading && !viewerError ? (
              <pre className="code file-viewer-content">{viewerContent}</pre>
            ) : null}
            {!viewerLoading && !viewerError && viewerTruncated ? (
              <div className="subtle">文件过大，已截断展示。</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  )
}
