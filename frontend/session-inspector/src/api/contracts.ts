export interface SessionSummary {
  session_id: string
  session_dir: string
  start_ts: string
  end_ts: string
  turn_count: number
  formats: string[]
}

export interface SessionsResponse {
  items: SessionSummary[]
  next_cursor: string | null
}

export interface TimelineLane {
  lane_id: string
  label: string
  event_count: number
  first_ts: string
  last_ts: string
}

export interface ToolDefinition {
  name?: string
  description?: string
  parameters?: unknown
  [key: string]: unknown
}

export interface TimelineEvent {
  event_id: string
  ts: string
  lane_id: string
  kind: string
  summary: string
  detail: unknown
  tool_name?: string | null
  tool_args?: unknown
  tool_def?: ToolDefinition | null
  source_files?: {
    request?: string | null
    response?: string | null
    non_stream_response?: string | null
    downstream_response?: string | null
  } | null
  turn_ts: string
  format: string
}

export interface TimelineResponse {
  session_id: string
  session_dir: string
  lanes: TimelineLane[]
  events: TimelineEvent[]
  stats: {
    total_events: number
    tool_events: number
    non_tool_events: number
    lane_count: number
    filtered_scope?: {
      turn_count_after_keywords: number
      session_tokens: {
        input_tokens: number
        output_tokens: number
        num_turns: number
      }
      tool_calls: {
        total_calls: number
        by_tool: Array<{
          tool_name: string
          count: number
        }>
      }
      agents: Array<{
        lane_id: string
        label: string
        tokens: {
          input_tokens: number
          output_tokens: number
          num_turns: number
        }
        tool_calls_total: number
        tool_calls_by_name: Array<{
          tool_name: string
          count: number
        }>
      }>
    }
  }
  meta: {
    warnings: string[]
    summary_chars: number
  }
}

export interface KeywordPreset {
  id: string
  name: string
  include_keywords: string[]
  exclude_keywords: string[]
  updated_at: string
}

export interface KeywordPresetsResponse {
  version: number
  default_preset_id: string | null
  presets: KeywordPreset[]
}

export interface LogFileContentResponse {
  path: string
  content: string
  size_bytes: number
  truncated: boolean
}
