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
  }
  meta: {
    warnings: string[]
    summary_chars: number
  }
}
