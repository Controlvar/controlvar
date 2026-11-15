/**
 * The `mcp:read` API-key scope: its value, checkbox label and help text, in
 * one place. The Admin -> API Keys dialog uses them, and any other screen that
 * offers the same checkbox imports them from here instead of copying the words.
 */
export const MCP_READ_SCOPE = 'mcp:read';

export const MCP_READ_SCOPE_LABEL = 'Read flags and experiments (mcp:read)';

/**
 * What the key reads, and who can read it. It gives no install or connect
 * steps.
 */
export const MCP_READ_SCOPE_HELP =
  'For tools such as the Controlvar MCP server. A key with this scope can list feature flags and experiments and read an experiment and its results. Anyone who holds the key reads everything its owner can read, so keep it secret and out of browser and mobile apps.';
