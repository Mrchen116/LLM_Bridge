import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import type {
  TimelineEvent,
  ToolTokenTimelineItem,
  ToolTokenTimelineResponse,
  ToolDefinition,
  TokenBreakdownResponse,
} from '../../api/contracts'
import {
  fetchTextTokenCount,
  fetchLogFileContent,
  fetchRequestCopyCompression,
  fetchTimelineEventDetail,
  fetchToolTokenTimeline,
  fetchTokenBreakdown,
} from '../../api/session-inspector-client'
import { extractEventMainText, formatCodeValue, normalizeEscapedText, normalizeReadableText } from '../../lib/event-display'

interface DetailPanelProps {
  event: TimelineEvent | null
  sessionId: string
  onResizeStart?: (startX: number) => void
}

type BlockVariant = 'text' | 'code' | 'markdown'
type FileViewerMode = 'rendered' | 'raw'
type JsonTreeData = Record<string, unknown> | unknown[]

type JsonEditorInstance = {
  destroy: () => void
  set: (json: JsonTreeData) => void
  expandAll: () => void
  node?: JsonEditorNode
}

type JsonEditorConstructor = new (
  container: HTMLElement,
  options: {
    mode: 'view'
    mainMenuBar: boolean
    navigationBar: boolean
    search: boolean
  },
) => JsonEditorInstance

type JsonEditorNode = {
  parent?: JsonEditorNode | null
  type?: string
  getPath: () => Array<string | number>
}

type JsonEditorRow = HTMLTableRowElement & {
  node?: JsonEditorNode
}

type JsonSourceMapLoc = {
  line: number
  column: number
  pos: number
}

type JsonSourceMapPointer = {
  value?: JsonSourceMapLoc
  valueEnd?: JsonSourceMapLoc
}

type RequestCopyJsonSourceMapModule = {
  parse: (source: string) => {
    pointers: Record<string, JsonSourceMapPointer>
  }
}

type RawJsonPathEntry = {
  pathSegments: string[]
  stickySegments: string[]
  startLine: number
  endLine: number
  depth: number
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isJsonTreeData(value: unknown): value is JsonTreeData {
  return Array.isArray(value) || isObjectRecord(value)
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

function normalizeMarkdownSource(value: unknown): string {
  let text = typeof value === 'string' ? value : formatCopyValue(value)
  for (let index = 0; index < 4; index += 1) {
    const normalized = normalizeReadableText(text)
    if (normalized === text) {
      break
    }
    text = normalized
  }

  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const indents = lines
    .filter((line) => line.trim().length > 0)
    .map((line) => line.match(/^\s*/)![0].length)
  const minIndent = indents.length > 0 ? Math.min(...indents) : 0
  const outdented =
    minIndent > 0
      ? lines.map((line) => (line.trim().length > 0 ? line.slice(minIndent) : line)).join('\n')
      : lines.join('\n')

  return outdented
    .replace(/\u00a0/g, ' ')
    .replace(/\\([`*_{}[\]()#+\-.!>~|])/g, '$1')
}

function formatDisplayValue(value: unknown, variant: BlockVariant): string {
  if (variant === 'markdown') {
    return normalizeMarkdownSource(value)
  }
  if (variant === 'text') {
    return normalizeReadableText(typeof value === 'string' ? value : formatCopyValue(value))
  }
  return formatCodeValue(value)
}

function tryParseJsonTree(content: string): JsonTreeData | null {
  const trimmed = content.trim()
  if (!trimmed || (!trimmed.startsWith('{') && !trimmed.startsWith('['))) {
    return null
  }

  try {
    const parsed = JSON.parse(content)
    return isJsonTreeData(parsed) ? parsed : null
  } catch {
    return null
  }
}

function applyRenderedJsonTreeStrings(container: HTMLElement): void {
  const stringNodes = container.querySelectorAll<HTMLElement>('.jsoneditor-value.jsoneditor-string')
  stringNodes.forEach((node) => {
    const currentText = node.textContent ?? ''
    const normalizedText = normalizeEscapedText(currentText)
    if (normalizedText !== currentText) {
      node.textContent = normalizedText
    }
  })
}

function formatJsonPathSegment(segment: string | number): string {
  return typeof segment === 'number' ? `[${segment}]` : segment
}

function decodeJsonPointerSegment(segment: string): string {
  return segment.replace(/~1/g, '/').replace(/~0/g, '~')
}

function jsonPointerToSegments(pointer: string): string[] {
  if (!pointer) {
    return []
  }
  return pointer
    .split('/')
    .slice(1)
    .map((segment) => decodeJsonPointerSegment(segment))
}

function decodeJsonPointerToken(token: string): string {
  return token.replace(/~1/g, '/').replace(/~0/g, '~')
}

function setJsonValueByPointer(target: unknown, pointer: string, value: unknown): boolean {
  if (!pointer.startsWith('/')) {
    return false
  }

  const segments = pointer
    .split('/')
    .slice(1)
    .map((segment) => decodeJsonPointerToken(segment))
  if (segments.length === 0) {
    return false
  }

  let current = target
  for (let index = 0; index < segments.length - 1; index += 1) {
    const segment = segments[index]
    if (Array.isArray(current)) {
      const itemIndex = Number(segment)
      if (!Number.isInteger(itemIndex)) {
        return false
      }
      current = current[itemIndex]
      continue
    }
    if (!isObjectRecord(current)) {
      return false
    }
    current = current[segment]
  }

  const lastSegment = segments[segments.length - 1]
  if (Array.isArray(current)) {
    const itemIndex = Number(lastSegment)
    if (!Number.isInteger(itemIndex)) {
      return false
    }
    current[itemIndex] = value
    return true
  }
  if (!isObjectRecord(current)) {
    return false
  }
  current[lastSegment] = value
  return true
}

function getFileName(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  const parts = normalized.split('/')
  return parts[parts.length - 1] || path
}

function formatCompressedToolResultPlaceholder(path: string, startLine: number, endLine: number): string {
  const lineCount = Math.max(1, endLine - startLine + 1)
  return `[compressed tool result] ${getFileName(path)}:${startLine}-${endLine} (${lineCount} lines)`
}

function formatBytes(chars: number): string {
  if (chars >= 1_000_000) {
    return `${(chars / 1_000_000).toFixed(2)}MB`
  }
  if (chars >= 1_000) {
    return `${(chars / 1_000).toFixed(1)}KB`
  }
  return `${chars}B`
}

function replaceImageBlocks(value: unknown): [unknown, boolean] {
  if (typeof value !== 'object' || value === null) {
    return [value, false]
  }
  if (Array.isArray(value)) {
    let replacedAny = false
    const newArr = value.map((item) => {
      const [replaced, didReplace] = replaceImageBlocks(item)
      if (didReplace) replacedAny = true
      return replaced
    })
    return [newArr, replacedAny]
  }
  const obj = value as Record<string, unknown>
  if (obj.type === 'image' && isObjectRecord(obj.source)) {
    const src = obj.source as Record<string, unknown>
    const data = typeof src.data === 'string' ? src.data : ''
    if (data.length > 0) {
      const placeholder = `[image: ${formatBytes(data.length)} base64 data]`
      return [
        {
          ...obj,
          source: {
            ...src,
            data: placeholder,
          },
        },
        true,
      ]
    }
  }
  let replacedAny = false
  const newObj: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(obj)) {
    const [replaced, didReplace] = replaceImageBlocks(v)
    newObj[k] = replaced
    if (didReplace) replacedAny = true
  }
  return [newObj, replacedAny]
}

function formatCompressedRequestCopyNote(
  thresholdTokens: number,
  requestAbsolutePath: string,
  nonStreamAbsolutePath?: string | null,
  hasImages?: boolean,
): string {
  const lines = [
    '',
    '[Note] [request] is the request log file shown above.',
    `[Note] [request] absolute path: ${requestAbsolutePath}`,
  ]
  if (nonStreamAbsolutePath) {
    lines.push('[Note] [non-stream-res] is the non-stream response log file shown above.')
    lines.push(`[Note] [non-stream-res] absolute path: ${nonStreamAbsolutePath}`)
  }
  lines.push(
    '[Note] Some tool results were compressed in this copied text because a single tool result exceeded '
      + `${thresholdTokens} tokens. The original agent saw the full, uncompressed results.`
  )
  lines.push(
    "[Note] The compression exists only to reduce noise for trajectory analysis. Including the full uncompressed tool outputs here would add too much irrelevant context and make it harder to analyze how the agent actually operated from start to finish."
  )
  if (hasImages) {
    lines.push('[Note] Image base64 data was replaced with placeholders in this copied text. The original agent saw the full image data.')
  }
  lines.push('[Note] If you need a compressed tool result, read only the exact referenced line range in [request]. Do not widen the range.')
  lines.push('[Note] Do not widen the line range. Do not read the full file. Do not read a broader surrounding block.')
  return lines.join('\n')
}

function formatCopySection(title: string, content: string): string {
  return [`[${title}]`, content].join('\n')
}

function tryCompactJsonText(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 0)
  } catch {
    return content
  }
}

async function buildCompressedRequestCopyText(
  sessionId: string,
  eventId: string,
): Promise<string> {
  const compression = await fetchRequestCopyCompression(sessionId, eventId, 500)
  const logFile = await fetchLogFileContent(compression.request_path)
  const requestContent = logFile.content

  let parsedRequest: unknown
  try {
    parsedRequest = JSON.parse(requestContent)
  } catch {
    const sections = [formatCopySection('request', requestContent)]
    if (compression.non_stream_response_path) {
      const nonStreamLogFile = await fetchLogFileContent(compression.non_stream_response_path)
      sections.push(formatCopySection('non-stream-res', tryCompactJsonText(nonStreamLogFile.content)))
    }
    sections.push(
      formatCompressedRequestCopyNote(
        compression.threshold_tokens,
        compression.request_absolute_path,
        compression.non_stream_response_absolute_path,
      ),
    )
    return sections.join('\n\n')
  }

  const [requestWithImagePlaceholders, hasImages] = replaceImageBlocks(parsedRequest)

  let requestOutput = JSON.stringify(requestWithImagePlaceholders, null, 0)
  if (Array.isArray(compression.compressed_items) && compression.compressed_items.length > 0) {
    const sourceMapModule = (await import('json-source-map')) as RequestCopyJsonSourceMapModule
    const pointerMap = sourceMapModule.parse(requestContent).pointers
    for (const item of compression.compressed_items) {
      const loc = pointerMap[item.pointer]
      const startLine = (loc?.value?.line ?? 0) + 1
      const endLine = (loc?.valueEnd?.line ?? loc?.value?.line ?? 0) + 1
      const replacement = formatCompressedToolResultPlaceholder(logFile.path, startLine, endLine)
      setJsonValueByPointer(requestWithImagePlaceholders, item.pointer, replacement)
    }

    requestOutput = JSON.stringify(requestWithImagePlaceholders, null, 0)
  }

  const sections = [formatCopySection('request', requestOutput)]
  if (compression.non_stream_response_path) {
    const nonStreamLogFile = await fetchLogFileContent(compression.non_stream_response_path)
    sections.push(formatCopySection('non-stream-res', tryCompactJsonText(nonStreamLogFile.content)))
  }
  sections.push(
    formatCompressedRequestCopyNote(
      compression.threshold_tokens,
      compression.request_absolute_path,
      compression.non_stream_response_absolute_path,
      hasImages,
    ),
  )
  return sections.join('\n\n')
}

function getJsonValueBySegments(data: unknown, segments: string[]): unknown {
  let current = data
  for (const segment of segments) {
    if (Array.isArray(current)) {
      const index = Number(segment)
      current = Number.isInteger(index) ? current[index] : undefined
      continue
    }
    if (isObjectRecord(current)) {
      current = current[segment]
      continue
    }
    return undefined
  }
  return current
}

function isContainerValue(value: unknown): boolean {
  return Array.isArray(value) || isObjectRecord(value)
}

function isExpandableJsonNode(node: JsonEditorNode | null | undefined): boolean {
  return node?.type === 'array' || node?.type === 'object'
}

function getStickyPathSegments(node: JsonEditorNode | null | undefined): string[] {
  if (!node) {
    return []
  }

  const targetNode = isExpandableJsonNode(node) ? node : (node.parent ?? null)
  if (!targetNode) {
    return []
  }

  return targetNode.getPath().map((segment) => formatJsonPathSegment(segment))
}

function arePathSegmentsEqual(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function LineNumberedCodeBlock({
  content,
  className,
  contentRef,
}: {
  content: string
  className?: string
  contentRef?: React.RefObject<HTMLDivElement | null>
}) {
  const lines = content.replace(/\r\n/g, '\n').split('\n')

  return (
    <div className={`line-numbered-code${className ? ` ${className}` : ''}`} ref={contentRef}>
      {lines.map((line, index) => (
        <div className="line-numbered-code-row" key={`line-${index + 1}`}>
          <span className="line-numbered-code-gutter" aria-hidden="true">
            {index + 1}
          </span>
          <span className="line-numbered-code-text">{line.length > 0 ? line : ' '}</span>
        </div>
      ))}
    </div>
  )
}

function RawJsonViewer({ content, data }: { content: string; data: JsonTreeData | null }) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const preRef = useRef<HTMLDivElement | null>(null)
  const stickyRef = useRef<HTMLDivElement | null>(null)
  const pathEntriesRef = useRef<RawJsonPathEntry[]>([])
  const [stickyPathSegments, setStickyPathSegments] = useState<string[]>([])

  useEffect(() => {
    let cancelled = false

    if (!data) {
      pathEntriesRef.current = []
      setStickyPathSegments([])
      return
    }

    void import('json-source-map')
      .then((module) => {
        if (cancelled) {
          return
        }

        const parser = (
          module as unknown as {
            parse: (source: string) => {
              pointers: Record<string, JsonSourceMapPointer>
            }
          }
        ).parse

        const parsed = parser(content)
        const entries = Object.entries(parsed.pointers)
          .map(([pointer, loc]) => {
            const pathSegments = jsonPointerToSegments(pointer)
            const value = getJsonValueBySegments(data, pathSegments)
            const stickySegments = isContainerValue(value)
              ? pathSegments.map((segment) => formatJsonPathSegment(segment))
              : pathSegments.slice(0, -1).map((segment) => formatJsonPathSegment(segment))

            return {
              pathSegments,
              stickySegments,
              startLine: loc.value?.line ?? 0,
              endLine: loc.valueEnd?.line ?? loc.value?.line ?? 0,
              depth: pathSegments.length,
            }
          })
          .sort((left, right) => {
            if (left.startLine !== right.startLine) {
              return left.startLine - right.startLine
            }
            return right.depth - left.depth
          })

        pathEntriesRef.current = entries
      })
      .catch(() => {
        pathEntriesRef.current = []
        setStickyPathSegments([])
      })

    return () => {
      cancelled = true
    }
  }, [content, data])

  useEffect(() => {
    const scrollEl = scrollRef.current
    const preEl = preRef.current
    if (!scrollEl || !preEl) {
      return
    }

    const getLineHeight = () => {
      const computed = window.getComputedStyle(preEl)
      const parsed = Number.parseFloat(computed.lineHeight)
      return Number.isFinite(parsed) && parsed > 0 ? parsed : 18
    }

    let rafId = 0
    const updateStickyPath = () => {
      const lineHeight = getLineHeight()
      const stickyHeight = stickyRef.current?.offsetHeight ?? 0
      const currentLine = Math.max(0, Math.floor((scrollEl.scrollTop + stickyHeight) / lineHeight))
      const entries = pathEntriesRef.current

      let nextSegments: string[] = []
      for (const entry of entries) {
        if (entry.startLine <= currentLine && entry.endLine >= currentLine) {
          nextSegments = entry.stickySegments
          break
        }
      }

      if (nextSegments.length === 0) {
        for (let index = entries.length - 1; index >= 0; index -= 1) {
          const entry = entries[index]
          if (entry.startLine <= currentLine) {
            nextSegments = entry.stickySegments
            break
          }
        }
      }

      setStickyPathSegments((current) => (arePathSegmentsEqual(current, nextSegments) ? current : nextSegments))
    }

    const onScroll = () => {
      if (rafId) {
        window.cancelAnimationFrame(rafId)
      }
      rafId = window.requestAnimationFrame(updateStickyPath)
    }

    scrollEl.addEventListener('scroll', onScroll)
    onScroll()

    return () => {
      scrollEl.removeEventListener('scroll', onScroll)
      if (rafId) {
        window.cancelAnimationFrame(rafId)
      }
    }
  }, [content, data])

  return (
    <div className="file-viewer-content file-viewer-raw-json" ref={scrollRef}>
      {stickyPathSegments.length > 0 ? (
        <div className="json-sticky-path" ref={stickyRef}>
          {stickyPathSegments.map((segment, index) => (
            <span className="json-sticky-path-segment" key={`${segment}-${index}`}>
              {index > 0 ? <span className="json-sticky-path-separator">/</span> : null}
              <span>{segment}</span>
            </span>
          ))}
        </div>
      ) : null}
      <LineNumberedCodeBlock content={content} className="raw-json-pre" contentRef={preRef} />
    </div>
  )
}

function JsonTreeViewer({ data, mode }: { data: JsonTreeData; mode: FileViewerMode }) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const stickyRef = useRef<HTMLDivElement | null>(null)
  const editorRef = useRef<JsonEditorInstance | null>(null)
  const latestDataRef = useRef<JsonTreeData>(data)
  const [loadError, setLoadError] = useState('')
  const [stickyPathSegments, setStickyPathSegments] = useState<string[]>([])

  latestDataRef.current = data

  const updateStickyPath = () => {
    const scrollEl = scrollRef.current
    const hostEl = containerRef.current
    if (!scrollEl || !hostEl) {
      return
    }

    const stickyHeight = stickyRef.current?.offsetHeight ?? 0
    const scrollRect = scrollEl.getBoundingClientRect()
    const targetTop = scrollRect.top + stickyHeight + 4
    const targetBottom = scrollRect.bottom
    const rows = Array.from(hostEl.querySelectorAll('tr')) as JsonEditorRow[]

    let nextSegments: string[] = []
    for (const row of rows) {
      const rect = row.getBoundingClientRect()
      if (rect.bottom > targetTop && rect.top < targetBottom) {
        nextSegments = getStickyPathSegments(row.node)
        break
      }
    }

    setStickyPathSegments((current) => (arePathSegmentsEqual(current, nextSegments) ? current : nextSegments))
  }

  useEffect(() => {
    let cancelled = false

    void import('jsoneditor')
      .then((module) => {
        if (cancelled || !containerRef.current) {
          return
        }

        const JSONEditor = (module.default ?? module) as JsonEditorConstructor
        const editor = new JSONEditor(containerRef.current, {
          mode: 'view',
          mainMenuBar: false,
          navigationBar: false,
          search: false,
        })

        editor.set(latestDataRef.current)
        editor.expandAll()
        editorRef.current = editor
        setLoadError('')
        updateStickyPath()
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : String(error))
        }
      })

    return () => {
      cancelled = true
      editorRef.current?.destroy()
      editorRef.current = null
    }
  }, [])

  useEffect(() => {
    editorRef.current?.set(data)
    editorRef.current?.expandAll()
    updateStickyPath()
  }, [data, mode])

  useEffect(() => {
    if (!containerRef.current) {
      return
    }

    if (mode === 'rendered') {
      applyRenderedJsonTreeStrings(containerRef.current)
    }

    const observer = new MutationObserver(() => {
      if (containerRef.current) {
        if (mode === 'rendered') {
          applyRenderedJsonTreeStrings(containerRef.current)
        }
        updateStickyPath()
      }
    })

    observer.observe(containerRef.current, {
      childList: true,
      subtree: true,
    })

    return () => observer.disconnect()
  }, [mode, data])

  useEffect(() => {
    const scrollEl = scrollRef.current
    if (!scrollEl) {
      return
    }

    let rafId = 0
    const onScroll = () => {
      if (rafId) {
        window.cancelAnimationFrame(rafId)
      }
      rafId = window.requestAnimationFrame(() => {
        updateStickyPath()
      })
    }

    scrollEl.addEventListener('scroll', onScroll)
    onScroll()

    return () => {
      scrollEl.removeEventListener('scroll', onScroll)
      if (rafId) {
        window.cancelAnimationFrame(rafId)
      }
    }
  }, [data, mode])

  if (loadError) {
    return <div className="subtle">JSON 视图加载失败：{loadError}</div>
  }

  return (
    <div
      className={`file-viewer-content file-viewer-json ${mode === 'rendered' ? 'rendered' : 'raw'}`}
      ref={scrollRef}
    >
      {stickyPathSegments.length > 0 ? (
        <div className="json-sticky-path" ref={stickyRef}>
          {stickyPathSegments.map((segment, index) => (
            <span className="json-sticky-path-segment" key={`${segment}-${index}`}>
              {index > 0 ? <span className="json-sticky-path-separator">/</span> : null}
              <span>{segment}</span>
            </span>
          ))}
        </div>
      ) : null}
      <div className="jsoneditor-host" ref={containerRef} />
    </div>
  )
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
  copied: string
  onCopy: (text: string) => void
}) {
  const copyText = formatCopyValue(value)
  const displayText = formatDisplayValue(value, variant)

  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">{title}</div>
        <button className="code-copy" type="button" onClick={() => onCopy(copyText)}>
          {copied || copyLabel}
        </button>
      </div>
      {variant === 'markdown' ? (
        <div className="detail-markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize, rehypeHighlight]}
          >
            {displayText}
          </ReactMarkdown>
        </div>
      ) : (
        <pre className={variant === 'text' ? 'detail-text' : 'code'}>{displayText}</pre>
      )}
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
  copied: string
  onCopy: () => void
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

  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">日志文件</div>
        <button className="code-copy" type="button" onClick={onCopy}>
          {copied || '复制压缩日志'}
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

const TOKEN_CATEGORY_COLORS: Record<string, string> = {
  system_prompt: '#5b7fa8',
  tool_definitions: '#7faac8',
  user_messages: '#5e9e78',
  tool_calls: '#c08050',
  tool_results: '#c06060',
  assistant_text: '#7060b0',
  assistant_reasoning: '#a090d0',
}

const TOKEN_CATEGORY_LABELS: Record<string, string> = {
  system_prompt: 'System Prompt',
  tool_definitions: 'Tool Definitions',
  user_messages: 'User Messages',
  tool_calls: 'Tool Calls',
  tool_results: 'Tool Results',
  assistant_text: 'Assistant Text',
  assistant_reasoning: 'Assistant Reasoning',
}

const TOOL_TIMELINE_COLORS = [
  '#0067b1',
  '#d65f00',
  '#00843d',
  '#c71938',
  '#6f2dbd',
  '#00857a',
  '#b0007a',
  '#8f6a00',
  '#2f6b00',
  '#005f73',
  '#9b2226',
  '#3346a8',
  '#bf3f00',
  '#147d64',
  '#7f1d7a',
  '#5c6f00',
]

function fmt(n: number): string {
  return n.toLocaleString()
}

function pct(n: number, total: number): string {
  if (total === 0) return '0%'
  return ((n / total) * 100).toFixed(1) + '%'
}

function colorForToolName(toolName: string): string {
  let hash = 0
  for (let i = 0; i < toolName.length; i += 1) {
    hash = (hash * 31 + toolName.charCodeAt(i)) >>> 0
  }
  return TOOL_TIMELINE_COLORS[hash % TOOL_TIMELINE_COLORS.length]
}

function buildToolTimelineTitle(item: ToolTokenTimelineItem): string {
  return `${item.tool_name}: ${fmt(item.total_tokens)} tokens · args ${fmt(item.args_tokens)} / result ${fmt(item.result_tokens)}`
}

function groupToolTimelineItems(items: ToolTokenTimelineItem[]): Array<{
  toolName: string
  count: number
  totalTokens: number
  argsTokens: number
  resultTokens: number
}> {
  const byTool = new Map<
    string,
    {
      toolName: string
      count: number
      totalTokens: number
      argsTokens: number
      resultTokens: number
    }
  >()

  for (const item of items) {
    const key = item.tool_name || '(unknown)'
    const existing = byTool.get(key) ?? {
      toolName: key,
      count: 0,
      totalTokens: 0,
      argsTokens: 0,
      resultTokens: 0,
    }
    existing.count += 1
    existing.totalTokens += item.total_tokens
    existing.argsTokens += item.args_tokens
    existing.resultTokens += item.result_tokens
    byTool.set(key, existing)
  }

  return [...byTool.values()].sort((a, b) => b.totalTokens - a.totalTokens || a.toolName.localeCompare(b.toolName))
}

function TokenBreakdownBlock({
  sessionId,
  eventId,
}: {
  sessionId: string
  eventId: string
}) {
  const [data, setData] = useState<TokenBreakdownResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const onToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    const isOpen = e.currentTarget.open
    if (isOpen && !data && !loading && !error) {
      setLoading(true)
      void fetchTokenBreakdown(sessionId, eventId)
        .then((payload) => {
          setData(payload)
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : String(err))
        })
        .finally(() => {
          setLoading(false)
        })
    }
  }

  const b = data?.breakdown
  const categories = b
    ? ([
        ['system_prompt', b.system_prompt],
        ['tool_definitions', b.tool_definitions],
        ['user_messages', b.user_messages],
        ['tool_calls', b.tool_calls],
        ['tool_results', b.tool_results.total],
        ['assistant_text', b.assistant_text],
        ['assistant_reasoning', b.assistant_reasoning],
      ] as [string, number][]).filter(([, v]) => v > 0)
    : []
  const estimatedTotal = data?.estimated_total ?? 0

  return (
    <details className="raw-event token-breakdown-details" onToggle={onToggle}>
      <summary>Token 分布</summary>

      {loading && <div className="subtle token-breakdown-body">计算中...</div>}
      {!loading && error && <div className="subtle token-breakdown-body">加载失败：{error}</div>}

      {!loading && data && (
        <div className="token-breakdown-body">
          {/* Header: total tokens */}
          <div className="token-breakdown-header">
            <span className="token-breakdown-total">
              {fmt(data.total_input_tokens)} input tokens
            </span>
            <span className="token-breakdown-note">
              {data.total_from_api
                ? `API 实测 · 估算 ${fmt(data.estimated_total)}`
                : '全部为估算值'}
            </span>
          </div>

          {/* Stacked bar */}
          {estimatedTotal > 0 && (
            <div className="token-bar" title="Input token 分布">
              {categories.map(([key, val]) => (
                <div
                  key={key}
                  className="token-bar-segment"
                  style={{
                    width: pct(val, estimatedTotal),
                    background: TOKEN_CATEGORY_COLORS[key] ?? '#999',
                  }}
                  title={`${TOKEN_CATEGORY_LABELS[key] ?? key}: ${fmt(val)} (${pct(val, estimatedTotal)})`}
                />
              ))}
            </div>
          )}

          {/* Legend + values */}
          <div className="token-breakdown-rows">
            {categories.map(([key, val]) => (
              <div className="token-breakdown-row" key={key}>
                <span
                  className="token-breakdown-dot"
                  style={{ background: TOKEN_CATEGORY_COLORS[key] ?? '#999' }}
                />
                <span className="token-breakdown-label">{TOKEN_CATEGORY_LABELS[key] ?? key}</span>
                <span className="token-breakdown-bar-mini">
                  <span
                    className="token-breakdown-bar-fill"
                    style={{
                      width: pct(val, estimatedTotal),
                      background: TOKEN_CATEGORY_COLORS[key] ?? '#999',
                    }}
                  />
                </span>
                <span className="token-breakdown-count">{fmt(val)}</span>
                <span className="token-breakdown-pct">{pct(val, estimatedTotal)}</span>
              </div>
            ))}
          </div>

          {/* Tool results drill-down */}
          {b && b.tool_results.by_tool.length > 1 && (
            <div className="token-breakdown-tools">
              <div className="token-breakdown-tools-title">Tool Results 明细</div>
              {b.tool_results.by_tool.map((item) => (
                <div className="token-breakdown-row token-breakdown-row--tool" key={item.tool_name}>
                  <span className="token-breakdown-tool-name">{item.tool_name}</span>
                  <span className="token-breakdown-bar-mini">
                    <span
                      className="token-breakdown-bar-fill"
                      style={{
                        width: pct(item.tokens, b.tool_results.total),
                        background: TOKEN_CATEGORY_COLORS['tool_results'],
                      }}
                    />
                  </span>
                  <span className="token-breakdown-count">{fmt(item.tokens)}</span>
                  <span className="token-breakdown-pct">{pct(item.tokens, b.tool_results.total)}</span>
                </div>
              ))}
            </div>
          )}

          {/* Warnings */}
          {data.has_uncountable_image_content && (
            <div className="token-breakdown-warn">
              ⚠ 部分 Tool Results 包含图片数据（base64），已跳过计数，不计入上述估算，实际 Tool Results 占比可能更高。
            </div>
          )}
          {data.has_encrypted_reasoning && (
            <div className="token-breakdown-warn">
              ⚠ 存在加密 reasoning（如 Codex），其 token 数无法统计，未计入上述估算。
            </div>
          )}
        </div>
      )}
    </details>
  )
}

function ToolTokenTimelineBlock({
  sessionId,
  eventId,
}: {
  sessionId: string
  eventId: string
}) {
  const [data, setData] = useState<ToolTokenTimelineResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [activeToolName, setActiveToolName] = useState('')
  const onToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    const isOpen = e.currentTarget.open
    if (isOpen && !data && !loading && !error) {
      setLoading(true)
      void fetchToolTokenTimeline(sessionId, eventId)
        .then((payload) => {
          setData(payload)
          setActiveToolName('')
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : String(err))
        })
        .finally(() => {
          setLoading(false)
        })
    }
  }

  const totalTokens = data?.total_tokens ?? 0
  const groupedTools = useMemo(() => groupToolTimelineItems(data?.items ?? []), [data])

  useEffect(() => {
    if (!modalOpen) {
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setModalOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [modalOpen])

  return (
    <>
      <details className="raw-event tool-token-timeline-details" onToggle={onToggle}>
        <summary>工具 Token 时间轴</summary>

        {loading && <div className="subtle tool-token-timeline-body">计算中...</div>}
        {!loading && error && <div className="subtle tool-token-timeline-body">加载失败：{error}</div>}

        {!loading && data && (
          <div className="tool-token-timeline-body">
            <div className="tool-token-header">
              <div>
                <span className="token-breakdown-total">{fmt(totalTokens)} tool tokens</span>
                <span className="token-breakdown-note">
                  {data.items.length > 0 ? `${fmt(data.items.length)} 次工具调用` : '当前请求上下文没有已配对工具调用'}
                </span>
              </div>
              {data.items.length > 0 ? (
                <button className="btn ghost tool-token-expand-btn" type="button" onClick={() => setModalOpen(true)}>
                  全屏查看
                </button>
              ) : null}
            </div>

            {totalTokens > 0 ? (
              <div className="tool-token-track" title="按工具调用顺序展示 args + result tokens">
                {data.items.map((item) => {
                  const width = pct(item.total_tokens, totalTokens)
                  const color = colorForToolName(item.tool_name)
                  return (
                    <div
                      className="tool-token-segment"
                      key={`${item.sequence}-${item.call_id}`}
                      style={{ width, background: color }}
                      title={buildToolTimelineTitle(item)}
                    />
                  )
                })}
              </div>
            ) : null}

            {data.items.length > 0 ? (
              <div className="tool-token-rows">
                {data.items.map((item) => {
                  const color = colorForToolName(item.tool_name)
                  return (
                    <div className="tool-token-row" key={`tool-row-${item.sequence}-${item.call_id}`}>
                      <span className="token-breakdown-dot" style={{ background: color }} />
                      <span className="tool-token-name" title={item.tool_name}>
                        {item.tool_name}
                      </span>
                      <span className="token-breakdown-bar-mini">
                        <span
                          className="token-breakdown-bar-fill"
                          style={{ width: pct(item.total_tokens, totalTokens), background: color }}
                        />
                      </span>
                      <span className="tool-token-split">
                        {fmt(item.args_tokens)} + {fmt(item.result_tokens)}
                      </span>
                      <span className="token-breakdown-count">{fmt(item.total_tokens)}</span>
                    </div>
                  )
                })}
              </div>
            ) : null}

            {data.has_uncountable_image_content && (
              <div className="token-breakdown-warn">
                部分 Tool Results 包含图片数据（base64），已跳过计数，实际 result token 占比可能更高。
              </div>
            )}
          </div>
        )}
      </details>

      {modalOpen && data ? (
        <div className="tool-token-modal-backdrop" onClick={() => setModalOpen(false)}>
          <div className="tool-token-modal" onClick={(event) => event.stopPropagation()}>
            <div className="tool-token-modal-head">
              <div>
                <div className="tool-token-modal-title">工具 Token 时间轴</div>
                <div className="subtle">
                  {fmt(totalTokens)} tokens · {fmt(data.items.length)} 次工具调用
                  {activeToolName ? ` · 高亮 ${activeToolName}` : ''}
                </div>
              </div>
              <div className="tool-token-modal-actions">
                {activeToolName ? (
                  <button className="btn ghost" type="button" onClick={() => setActiveToolName('')}>
                    清除高亮
                  </button>
                ) : null}
                <button className="btn ghost" type="button" onClick={() => setModalOpen(false)}>
                  关闭
                </button>
              </div>
            </div>

            <div className="tool-token-modal-track-wrap">
              <div className="tool-token-modal-track" title="按工具调用顺序展示 args + result tokens">
                {data.items.map((item) => {
                  const color = colorForToolName(item.tool_name)
                  const isDimmed = Boolean(activeToolName && item.tool_name !== activeToolName)
                  return (
                    <button
                      className={`tool-token-modal-segment${isDimmed ? ' dimmed' : ''}${item.tool_name === activeToolName ? ' active' : ''}`}
                      key={`modal-segment-${item.sequence}-${item.call_id}`}
                      style={{ width: pct(item.total_tokens, totalTokens), background: color }}
                      title={buildToolTimelineTitle(item)}
                      type="button"
                      onClick={() => setActiveToolName(item.tool_name)}
                    />
                  )
                })}
              </div>
            </div>

            <div className="tool-token-modal-body">
              <section className="tool-token-modal-panel">
                <div className="tool-token-modal-panel-title">按时序</div>
                <div className="tool-token-modal-list">
                  {data.items.map((item) => {
                    const color = colorForToolName(item.tool_name)
                    const isDimmed = Boolean(activeToolName && item.tool_name !== activeToolName)
                    const isActive = item.tool_name === activeToolName
                    return (
                      <button
                        className={`tool-token-modal-row${isDimmed ? ' dimmed' : ''}${isActive ? ' active' : ''}`}
                        key={`modal-row-${item.sequence}-${item.call_id}`}
                        type="button"
                        onClick={() => setActiveToolName(item.tool_name)}
                      >
                        <span className="tool-token-modal-index">{item.sequence + 1}</span>
                        <span className="token-breakdown-dot" style={{ background: color }} />
                        <span className="tool-token-modal-name" title={item.tool_name}>
                          {item.tool_name}
                        </span>
                        <span className="tool-token-modal-meter">
                          <span style={{ width: pct(item.total_tokens, totalTokens), background: color }} />
                        </span>
                        <span className="tool-token-modal-split">
                          args {fmt(item.args_tokens)} · result {fmt(item.result_tokens)}
                        </span>
                        <span className="tool-token-modal-total">{fmt(item.total_tokens)}</span>
                      </button>
                    )
                  })}
                </div>
              </section>

              <aside className="tool-token-modal-panel tool-token-modal-panel--summary">
                <div className="tool-token-modal-panel-title">按工具聚合</div>
                <div className="tool-token-summary-list">
                  {groupedTools.map((tool) => {
                    const color = colorForToolName(tool.toolName)
                    const isActive = tool.toolName === activeToolName
                    const isDimmed = Boolean(activeToolName && !isActive)
                    return (
                      <button
                        className={`tool-token-summary-item${isDimmed ? ' dimmed' : ''}${isActive ? ' active' : ''}`}
                        key={`tool-summary-${tool.toolName}`}
                        type="button"
                        onClick={() => setActiveToolName(isActive ? '' : tool.toolName)}
                      >
                        <span className="token-breakdown-dot" style={{ background: color }} />
                        <span className="tool-token-summary-main">
                          <span className="tool-token-summary-name" title={tool.toolName}>
                            {tool.toolName}
                          </span>
                          <span className="tool-token-summary-meta">
                            {fmt(tool.count)} 次 · args {fmt(tool.argsTokens)} · result {fmt(tool.resultTokens)}
                          </span>
                        </span>
                        <span className="tool-token-summary-total">{fmt(tool.totalTokens)}</span>
                      </button>
                    )
                  })}
                </div>
              </aside>
            </div>
          </div>
        </div>
      ) : null}
    </>
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

function getGroupedEvents(event: TimelineEvent | null): TimelineEvent[] {
  if (!event || !Array.isArray(event.grouped_events)) {
    return []
  }
  return event.grouped_events
}

function hasUnloadedGroupedEvents(event: TimelineEvent): boolean {
  return getGroupedEvents(event).some((child) => child.detail_loaded === false)
}

function getEventApiId(event: TimelineEvent): string {
  return event.representative_event_id || event.event_id
}

function GroupedToolCallContent({
  events,
  copiedKey,
  copiedLabel,
  onCopy,
}: {
  events: TimelineEvent[]
  copiedKey: string
  copiedLabel: string
  onCopy: (key: string, text: string) => void
}) {
  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">事件内容</div>
      </div>
      <div className="grouped-event-list">
        {events.map((child, index) => {
          const toolDefFields = extractToolDefinitionFields(child.tool_def)
          const itemKey = child.event_id || `tool-call-${index}`
          return (
            <details className="grouped-event-item" key={itemKey}>
              <summary className="grouped-event-title">
                {index + 1}. {child.tool_name || 'unknown'}
              </summary>
              <DetailBlock
                title="工具参数"
                value={child.tool_args ?? {}}
                variant="code"
                copyLabel="复制"
                copied={copiedKey === `group_tool_args_${index}` ? copiedLabel : ''}
                onCopy={(text) => onCopy(`group_tool_args_${index}`, text)}
              />
              {child.tool_def ? (
                <>
                  <DetailBlock
                    title="工具定义 · 名称"
                    value={toolDefFields.name}
                    variant="text"
                    copyLabel="复制"
                    copied={copiedKey === `group_tool_def_name_${index}` ? copiedLabel : ''}
                    onCopy={(text) => onCopy(`group_tool_def_name_${index}`, text)}
                  />
                  <DetailBlock
                    title="工具定义 · 描述"
                    value={toolDefFields.description}
                    variant="markdown"
                    copyLabel="复制"
                    copied={copiedKey === `group_tool_def_description_${index}` ? copiedLabel : ''}
                    onCopy={(text) => onCopy(`group_tool_def_description_${index}`, text)}
                  />
                  <DetailBlock
                    title="工具定义 · 参数"
                    value={toolDefFields.parameters}
                    variant="code"
                    copyLabel="复制"
                    copied={copiedKey === `group_tool_def_parameters_${index}` ? copiedLabel : ''}
                    onCopy={(text) => onCopy(`group_tool_def_parameters_${index}`, text)}
                  />
                </>
              ) : null}
            </details>
          )
        })}
      </div>
    </div>
  )
}

function GroupedToolResultContent({
  events,
  copiedKey,
  copiedLabel,
  onCopy,
}: {
  events: TimelineEvent[]
  copiedKey: string
  copiedLabel: string
  onCopy: (key: string, text: string) => void
}) {
  return (
    <div className="detail-block">
      <div className="detail-block-head">
        <div className="detail-title">事件内容</div>
      </div>
      <div className="grouped-event-list">
        {events.map((child, index) => {
          const title = child.tool_name ? `${index + 1}. ${child.tool_name} result` : `${index + 1}. tool_result`
          const text = extractEventMainText(child)
          return (
            <details className="grouped-event-item" key={child.event_id || `tool-result-${index}`}>
              <summary className="grouped-event-title">{title}</summary>
              <DetailBlock
                title="结果"
                value={text}
                variant="text"
                copyLabel="复制"
                copied={copiedKey === `group_tool_result_${index}` ? copiedLabel : ''}
                onCopy={(copyText) => onCopy(`group_tool_result_${index}`, copyText)}
              />
            </details>
          )
        })}
      </div>
    </div>
  )
}

export function DetailPanel({ event, sessionId, onResizeStart }: DetailPanelProps) {
  const [copiedState, setCopiedState] = useState<{ key: string; label: string }>({ key: '', label: '' })
  const [viewerOpen, setViewerOpen] = useState(false)
  const [viewerPath, setViewerPath] = useState('')
  const [viewerContent, setViewerContent] = useState('')
  const [viewerMode, setViewerMode] = useState<FileViewerMode>('rendered')
  const [viewerTruncated, setViewerTruncated] = useState(false)
  const [viewerLoading, setViewerLoading] = useState(false)
  const [viewerError, setViewerError] = useState('')
  const [resolvedEvent, setResolvedEvent] = useState<TimelineEvent | null>(null)
  const [eventLoading, setEventLoading] = useState(false)
  const [eventError, setEventError] = useState('')
  const displayEvent = resolvedEvent ?? event
  const groupedEvents = useMemo(() => getGroupedEvents(displayEvent), [displayEvent])
  const isToolCallGroup = displayEvent?.kind === 'tool_call_group'
  const isToolResultGroup = displayEvent?.kind === 'tool_result_group'
  const mainText = useMemo(() => (displayEvent ? extractEventMainText(displayEvent) : ''), [displayEvent])
  const toolDefFields = useMemo(
    () => extractToolDefinitionFields(displayEvent?.tool_def),
    [displayEvent],
  )
  const viewerParsedJson = useMemo(() => tryParseJsonTree(viewerContent), [viewerContent])
  const viewerJsonData = useMemo(() => viewerParsedJson, [viewerParsedJson])
  const viewerDisplayContent = useMemo(() => {
    if (viewerMode === 'raw') {
      return viewerContent
    }
    return normalizeEscapedText(viewerContent)
  }, [viewerContent, viewerMode])

  useEffect(() => {
    if (!copiedState.key) {
      return
    }

    const timer = window.setTimeout(() => setCopiedState({ key: '', label: '' }), 3000)
    return () => window.clearTimeout(timer)
  }, [copiedState.key])

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

  useEffect(() => {
    if (!event) {
      setResolvedEvent(null)
      setEventLoading(false)
      setEventError('')
      return
    }

    setResolvedEvent(event)
    setEventError('')
    if (sessionId && hasUnloadedGroupedEvents(event)) {
      let cancelled = false
      setEventLoading(true)
      void Promise.all(
        getGroupedEvents(event).map((child) => {
          if (child.detail_loaded !== false) {
            return Promise.resolve(child)
          }
          return fetchTimelineEventDetail(sessionId, child.event_id).then((payload) => payload.event)
        }),
      )
        .then((children) => {
          if (cancelled) {
            return
          }
          setResolvedEvent({
            ...event,
            grouped_events: children,
            detail: { grouped_events: children },
            detail_loaded: true,
          })
        })
        .catch((error: unknown) => {
          if (!cancelled) {
            setEventError(error instanceof Error ? error.message : String(error))
          }
        })
        .finally(() => {
          if (!cancelled) {
            setEventLoading(false)
          }
        })

      return () => {
        cancelled = true
      }
    }
    if (event.detail_loaded !== false || !sessionId) {
      setEventLoading(false)
      return
    }

    let cancelled = false
    setEventLoading(true)
    void fetchTimelineEventDetail(sessionId, event.event_id)
      .then((payload) => {
        if (cancelled) {
          return
        }
        setResolvedEvent(payload.event)
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return
        }
        setEventError(error instanceof Error ? error.message : String(error))
      })
      .finally(() => {
        if (!cancelled) {
          setEventLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [event, sessionId])

  const onCopy = async (key: string, text: string) => {
    try {
      const [, tokens] = await Promise.all([
        navigator.clipboard.writeText(text),
        fetchTextTokenCount(text).catch(() => 0),
      ])
      setCopiedState({
        key,
        label: tokens > 0 ? `已复制 ${tokens} tokens` : '已复制',
      })
    } catch {
      setCopiedState({ key: '', label: '' })
    }
  }

  const onCopyCompressedRequest = async () => {
    if (!displayEvent || !sessionId) {
      return
    }
    try {
      const text = await buildCompressedRequestCopyText(sessionId, getEventApiId(displayEvent))
      const [, tokens] = await Promise.all([
        navigator.clipboard.writeText(text),
        fetchTextTokenCount(text).catch(() => 0),
      ])
      setCopiedState({
        key: 'source_files',
        label: tokens > 0 ? `已复制 ${tokens} tokens` : '已复制',
      })
    } catch {
      setCopiedState({ key: '', label: '' })
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
        {onResizeStart ? (
          <div
            className="panel-resize-handle"
            onMouseDown={(e) => {
              e.preventDefault()
              onResizeStart(e.clientX)
            }}
          />
        ) : null}
        <header className="panel-header">
          <div className="panel-title-group">
            <h2>事件详情</h2>
            <p className="subtle">展示结构化事件信息</p>
          </div>
        </header>

        <div className="detail-body">
          {!displayEvent ? <div className="subtle">点击事件查看详情</div> : null}

          {displayEvent ? (
            <>
              {eventLoading ? <div className="subtle">正在补充事件详情...</div> : null}
              {!eventLoading && eventError ? <div className="subtle">详情加载失败：{eventError}</div> : null}
              <DetailBlock
                title="事件"
                value={`${displayEvent.kind} · ${displayEvent.ts}`}
                variant="text"
                copyLabel="复制"
                copied={copiedState.key === 'kind' ? copiedState.label : ''}
                onCopy={(text) => void onCopy('kind', text)}
              />
              <DetailBlock
                title="摘要"
                value={displayEvent.summary || ''}
                variant="text"
                copyLabel="复制"
                copied={copiedState.key === 'summary' ? copiedState.label : ''}
                onCopy={(text) => void onCopy('summary', text)}
              />
              {isToolCallGroup ? (
                <GroupedToolCallContent
                  events={groupedEvents}
                  copiedKey={copiedState.key}
                  copiedLabel={copiedState.label}
                  onCopy={(key, text) => void onCopy(key, text)}
                />
              ) : isToolResultGroup ? (
                <GroupedToolResultContent
                  events={groupedEvents}
                  copiedKey={copiedState.key}
                  copiedLabel={copiedState.label}
                  onCopy={(key, text) => void onCopy(key, text)}
                />
              ) : (
                <DetailBlock
                  title="事件内容"
                  value={mainText}
                  variant={displayEvent.kind === 'assistant_text' ? 'markdown' : 'text'}
                  copyLabel="复制"
                  copied={copiedState.key === 'main_text' ? copiedState.label : ''}
                  onCopy={(text) => void onCopy('main_text', text)}
                />
              )}
              <SourceFilesBlock
                sourceFiles={displayEvent.source_files}
                copied={copiedState.key === 'source_files' ? copiedState.label : ''}
                onCopy={() => void onCopyCompressedRequest()}
                onView={onViewSourceFile}
              />

              {displayEvent.kind === 'tool_call' ? (
                <>
                  <DetailBlock
                    title="工具名"
                    value={displayEvent.tool_name || ''}
                    variant="text"
                    copyLabel="复制"
                    copied={copiedState.key === 'tool_name' ? copiedState.label : ''}
                    onCopy={(text) => void onCopy('tool_name', text)}
                  />
                  <DetailBlock
                    title="工具参数"
                    value={displayEvent.tool_args ?? {}}
                    variant="code"
                    copyLabel="复制"
                    copied={copiedState.key === 'tool_args' ? copiedState.label : ''}
                    onCopy={(text) => void onCopy('tool_args', text)}
                  />
                  {displayEvent.tool_def ? (
                    <>
                      <DetailBlock
                        title="工具定义 · 名称"
                        value={toolDefFields.name}
                        variant="text"
                        copyLabel="复制"
                        copied={copiedState.key === 'tool_def_name' ? copiedState.label : ''}
                        onCopy={(text) => void onCopy('tool_def_name', text)}
                      />
                      <DetailBlock
                        title="工具定义 · 描述"
                        value={toolDefFields.description}
                        variant="markdown"
                        copyLabel="复制"
                        copied={copiedState.key === 'tool_def_description' ? copiedState.label : ''}
                        onCopy={(text) => void onCopy('tool_def_description', text)}
                      />
                      <DetailBlock
                        title="工具定义 · 参数"
                        value={toolDefFields.parameters}
                        variant="code"
                        copyLabel="复制"
                        copied={copiedState.key === 'tool_def_parameters' ? copiedState.label : ''}
                        onCopy={(text) => void onCopy('tool_def_parameters', text)}
                      />
                    </>
                  ) : null}
                </>
              ) : null}

              <TokenBreakdownBlock key={displayEvent.event_id} sessionId={sessionId} eventId={getEventApiId(displayEvent)} />
              <ToolTokenTimelineBlock
                key={`tool-token-timeline-${displayEvent.event_id}`}
                sessionId={sessionId}
                eventId={getEventApiId(displayEvent)}
              />

              <details className="raw-event">
                <summary>完整事件 JSON</summary>
                <DetailBlock
                  title="完整事件"
                  value={displayEvent}
                  variant="code"
                  copyLabel="复制"
                  copied={copiedState.key === 'full' ? copiedState.label : ''}
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
              <div className="file-viewer-actions">
                <div className="viewer-mode-group" role="group" aria-label="日志内容显示模式">
                  <button
                    className={`viewer-mode-btn${viewerMode === 'rendered' ? ' active' : ''}`}
                    type="button"
                    onClick={() => setViewerMode('rendered')}
                  >
                    渲染换行
                  </button>
                  <button
                    className={`viewer-mode-btn${viewerMode === 'raw' ? ' active' : ''}`}
                    type="button"
                    onClick={() => setViewerMode('raw')}
                  >
                    原始文本
                  </button>
                </div>
                <button className="btn ghost" type="button" onClick={() => setViewerOpen(false)}>
                  关闭
                </button>
              </div>
            </div>
            <div className="file-viewer-path">{viewerPath}</div>
            {viewerLoading ? <div className="subtle">加载中...</div> : null}
            {!viewerLoading && viewerError ? (
              <div className="subtle">读取失败：{viewerError}</div>
            ) : null}
            {!viewerLoading && !viewerError ? (
              viewerMode === 'raw' && viewerParsedJson ? (
                <RawJsonViewer content={viewerContent} data={viewerParsedJson} />
              ) : viewerJsonData ? (
                <JsonTreeViewer data={viewerJsonData} mode={viewerMode} />
              ) : (
                <div className="file-viewer-content file-viewer-raw-text">
                  <LineNumberedCodeBlock content={viewerDisplayContent} className="file-viewer-plain-text" />
                </div>
              )
            ) : null}
            {!viewerLoading && !viewerError && !viewerJsonData && viewerContent.trim() ? (
              <div className="subtle">当前内容无法解析为 JSON，已回退为文本视图。</div>
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
