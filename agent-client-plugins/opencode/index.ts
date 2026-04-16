import type { PluginInput, Hooks } from "@opencode-ai/plugin"

const rootCache = new Map<string, string>()

async function resolveRootSessionId(input: PluginInput, sessionID: string): Promise<string> {
  const cached = rootCache.get(sessionID)
  if (cached !== undefined) return cached

  try {
    const result = await input.client.session.get({ path: { id: sessionID } })
    const parentID = result.data?.parentID
    if (parentID) {
      const root = await resolveRootSessionId(input, parentID)
      rootCache.set(sessionID, root)
      return root
    }
  } catch {
    // API unavailable or session not found — fall back to current sessionID.
  }

  rootCache.set(sessionID, sessionID)
  return sessionID
}

export default {
  id: "llm-bridge-session",
  server: async (input: PluginInput): Promise<Hooks> => {
    return {
      "chat.headers": async (hookInput, output) => {
        output.headers["X-Session-Id"] = await resolveRootSessionId(input, hookInput.sessionID)
      },
    }
  },
}
