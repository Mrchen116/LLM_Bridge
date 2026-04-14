import type { Plugin, PluginInput, Hooks } from "@opencode-ai/plugin"

export const id = "llm-bridge-session"

export const server: Plugin = async (input: PluginInput): Promise<Hooks> => {
  // Cache: sessionID → root sessionID
  // Prevents redundant API calls when the same session fires multiple LLM requests.
  const rootCache = new Map<string, string>()

  async function resolveRootSessionId(sessionID: string): Promise<string> {
    const cached = rootCache.get(sessionID)
    if (cached !== undefined) return cached

    try {
      const result = await input.client.session.get({ path: { id: sessionID } })
      const parentID = result.data?.parentID
      if (parentID) {
        const root = await resolveRootSessionId(parentID)
        rootCache.set(sessionID, root)
        return root
      }
    } catch {
      // API unavailable or session not found — fall back to current sessionID.
    }

    rootCache.set(sessionID, sessionID)
    return sessionID
  }

  return {
    "chat.headers": async (hookInput, output) => {
      output.headers["X-Session-Id"] = await resolveRootSessionId(hookInput.sessionID)
    },
  }
}
