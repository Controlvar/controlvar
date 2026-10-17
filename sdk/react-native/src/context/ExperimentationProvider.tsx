/**
 * ExperimentationProvider — wraps your app and supplies the SDK client via context.
 *
 * @example
 * ```tsx
 * import { ExperimentationProvider, ExperimentationClient } from '@controlvar/react-native-sdk';
 *
 * const client = new ExperimentationClient({
 *   apiKey: 'YOUR_API_KEY',
 *   baseUrl: 'https://api.controlvar.example.com',
 * });
 *
 * export default function App() {
 *   return (
 *     <ExperimentationProvider client={client} userId="user-123">
 *       <YourApp />
 *     </ExperimentationProvider>
 *   );
 * }
 * ```
 */

import React from 'react';
import { ExperimentationContext } from './ExperimentationContext';
import type { ExperimentationClient } from '../client';

export interface ExperimentationProviderProps {
  /** The initialised ExperimentationClient. */
  client: ExperimentationClient;
  /** The current user's unique identifier. */
  userId: string;
  /** Optional user attributes for targeting rules. */
  attributes?: Record<string, unknown>;
  children: React.ReactNode;
}

/**
 * Provides the ExperimentationClient and user context to the React component
 * tree. All child components can access these via {@link useFlag},
 * {@link useExperiment}, or {@link useExperimentationClient}.
 */
export function ExperimentationProvider({
  client,
  userId,
  attributes,
  children,
}: ExperimentationProviderProps): React.ReactElement {
  const value = React.useMemo(
    () => ({ client, userId, attributes }),
    [client, userId, attributes]
  );

  return (
    <ExperimentationContext.Provider value={value}>
      {children}
    </ExperimentationContext.Provider>
  );
}
