import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

export default definePluginEntry({
  id: "session-header",
  name: "Session Header",
  description: "Injects X-Session-Id header into LLM_Bridge provider requests for session tracking",

  register: (api) => {
    api.registerProvider({
      id: "llm-bridge",
      label: "LLM Bridge",

      // No auth or catalog needed — user already configured this provider in openclaw config.
      // This plugin only hooks into the existing provider to inject the session header.

      wrapStreamFn: (ctx) => {
        if (!ctx.streamFn) return undefined;
        const inner = ctx.streamFn;
        return (model, context, options) => {
          const sessionId = (options as Record<string, unknown> | undefined)?.sessionId;
          if (!sessionId || typeof sessionId !== "string") {
            return inner(model, context, options);
          }
          return inner(model, context, {
            ...options,
            headers: {
              ...(options as Record<string, unknown>)?.headers as Record<string, string> | undefined,
              "X-Session-Id": sessionId,
            },
          });
        };
      },
    });
  },
});
