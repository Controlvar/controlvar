/**
 * Warehouse (beta): connections, assignment sources and metric sources.
 *
 * Who sees what follows the API's role matrix (services/warehouseRoles.ts):
 * VIEWER sees a notice and nothing is requested; ANALYST reads connections
 * and sources and may define metric sources; DEVELOPER also defines
 * assignment sources; only ADMIN creates connections. A connector this
 * deployment has not enabled is listed as not yet available, and no one is
 * offered a way to create a connection of that type.
 */
import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { withModule } from '@/components/ModuleNotice';
import { PageTitle } from '@/components/PageTitle';
import { useAuth } from '@/contexts/AuthContext';
import {
  Connection,
  Connector,
  Source,
  SourceKind,
  warehouseName,
  warehouseService,
} from '@modules/services/warehouse';
import { can, refusal, sourceAction } from '@modules/services/warehouseRoles';
import {
  CREDENTIALS_STATUS_TEXT,
  LoadError,
  Loading,
  NO_CONNECTOR_TEXT,
  RoleNotice,
  WAREHOUSE_MODULE_NOTICE,
  UtcTime,
  WarehouseHeader,
  errorText,
} from '@modules/components/warehouse/common';
import { Tabs } from '@modules/components/warehouse/Tabs';
import { formatBytes, formatSeconds } from '@modules/components/warehouse/validation';

type TabKey = 'connections' | 'assignment' | 'metric';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'connections', label: 'Connections' },
  { key: 'assignment', label: 'Assignment sources' },
  { key: 'metric', label: 'Metric sources' },
];

interface Data {
  connectors: Connector[];
  connections: Connection[];
  sources: Source[];
}

const LINK_BUTTON =
  'inline-flex rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800';

function ConnectorAvailability({ connectors }: { connectors: Connector[] }) {
  const none = connectors.every((c) => !c.enabled);
  return (
    <section aria-labelledby="wh-connectors-heading" className="mb-6 rounded-md border border-slate-200 bg-white p-4">
      <h2 id="wh-connectors-heading" className="text-sm font-semibold text-slate-900">
        Connectors
      </h2>
      <ul className="mt-2 flex flex-wrap gap-2" data-testid="warehouse-connectors">
        {connectors.map((c) => (
          <li
            key={c.warehouse_type}
            data-testid={`warehouse-connector-${c.warehouse_type}`}
            className="rounded-md border border-slate-200 px-2 py-1 text-sm text-slate-800"
          >
            {c.name}: <span className="font-medium">{c.enabled ? 'Available' : 'Not yet available'}</span>
          </li>
        ))}
      </ul>
      {none && (
        <p data-testid="warehouse-no-connector" className="mt-3 text-sm text-slate-700">
          {NO_CONNECTOR_TEXT}
        </p>
      )}
    </section>
  );
}

function ConnectionsPanel({ data }: { data: Data }) {
  const { user } = useAuth();
  const anyEnabled = data.connectors.some((c) => c.enabled);
  let action: React.ReactNode;
  if (!can(user, 'createConnection')) {
    action = <RoleNotice message={refusal(user, 'createConnection')} />;
  } else if (!anyEnabled) {
    action = (
      <p data-testid="warehouse-create-unavailable" className="text-sm text-slate-700">
        New connections can be created once a connector is available.
      </p>
    );
  } else {
    action = (
      <Link href="/warehouse/connections/new" className={LINK_BUTTON} data-testid="warehouse-new-connection">
        New connection
      </Link>
    );
  }
  return (
    <div className="space-y-4">
      <ConnectorAvailability connectors={data.connectors} />
      <div>{action}</div>
      {data.connections.length === 0 ? (
        <p data-testid="warehouse-connections-empty" className="py-6 text-sm text-slate-700">
          No warehouse connections yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm" data-testid="warehouse-connections-table">
            <caption className="sr-only">Warehouse connections</caption>
            <thead>
              <tr className="text-left text-slate-700">
                <th scope="col" className="py-2 pr-4 font-medium">Name</th>
                <th scope="col" className="py-2 pr-4 font-medium">Warehouse</th>
                <th scope="col" className="py-2 pr-4 font-medium">Status</th>
                <th scope="col" className="py-2 pr-4 font-medium">Limits</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.connections.map((c) => (
                <tr key={c.id}>
                  <td className="py-2 pr-4">
                    <Link href={`/warehouse/connections/${c.id}`} className="font-medium text-blue-700 hover:underline">
                      {c.name}
                    </Link>
                  </td>
                  <td className="py-2 pr-4 text-slate-800">
                    {warehouseName(c.warehouse_type)}
                    {!c.enabled && <span className="ml-2 text-xs text-slate-700">(not yet available)</span>}
                  </td>
                  <td className="py-2 pr-4 text-slate-800">{CREDENTIALS_STATUS_TEXT[c.credentials_status]}</td>
                  <td className="py-2 pr-4 text-slate-800">
                    {c.max_runs_per_day}/day
                    {c.max_bytes_per_query !== null && <> · {formatBytes(c.max_bytes_per_query)} per query</>}
                    {' · '}
                    {formatSeconds(c.query_timeout_seconds)} per query
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SourcesPanel({ data, kind }: { data: Data; kind: SourceKind }) {
  const { user } = useAuth();
  const noun = kind === 'assignment' ? 'assignment source' : 'metric source';
  const article = kind === 'assignment' ? 'An' : 'A';
  const sources = data.sources.filter((s) => s.kind === kind);
  const byId = new Map(data.connections.map((c) => [c.id, c]));
  let action: React.ReactNode;
  if (!can(user, sourceAction('create', kind))) {
    action = <RoleNotice message={refusal(user, sourceAction('create', kind))} />;
  } else if (data.connections.length === 0) {
    action = (
      <p className="text-sm text-slate-700" data-testid="warehouse-source-needs-connection">
        {article} {noun} reads a table through a warehouse connection. There are no connections yet
        {can(user, 'createConnection') ? ': create one on the Connections tab.' : '; an admin can create one.'}
      </p>
    );
  } else {
    action = (
      <Link href={`/warehouse/sources/new?kind=${kind}`} className={LINK_BUTTON} data-testid={`warehouse-new-${kind}-source`}>
        New {noun}
      </Link>
    );
  }
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-700">
        {kind === 'assignment'
          ? 'An assignment source is the table that records which user was assigned which variant of an experiment.'
          : 'A metric source is the table of events or values an analysis measures, per user.'}
      </p>
      <div>{action}</div>
      {sources.length === 0 ? (
        <p data-testid={`warehouse-${kind}-empty`} className="py-6 text-sm text-slate-700">
          No {noun}s yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm" data-testid={`warehouse-${kind}-table`}>
            <caption className="sr-only">{kind === 'assignment' ? 'Assignment sources' : 'Metric sources'}</caption>
            <thead>
              <tr className="text-left text-slate-700">
                <th scope="col" className="py-2 pr-4 font-medium">Name</th>
                <th scope="col" className="py-2 pr-4 font-medium">Connection</th>
                <th scope="col" className="py-2 pr-4 font-medium">Table or view</th>
                {kind === 'metric' && <th scope="col" className="py-2 pr-4 font-medium">Type</th>}
                <th scope="col" className="py-2 pr-4 font-medium">Validated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {sources.map((s) => (
                <tr key={s.id}>
                  <td className="py-2 pr-4">
                    <Link href={`/warehouse/sources/${s.id}`} className="font-medium text-blue-700 hover:underline">
                      {s.name}
                    </Link>
                  </td>
                  <td className="py-2 pr-4 text-slate-800">{byId.get(s.connection_id)?.name ?? '—'}</td>
                  <td className="py-2 pr-4 font-mono text-xs text-slate-800">{s.table}</td>
                  {kind === 'metric' && (
                    <td className="py-2 pr-4 text-slate-800">{s.metric_type === 'mean' ? 'Mean' : 'Proportion'}</td>
                  )}
                  <td className="py-2 pr-4 text-slate-800">
                    {s.validated_at ? <UtcTime value={s.validated_at} /> : 'Not validated'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function WarehousePage() {
  const { user } = useAuth();
  const router = useRouter();
  const allowed = can(user, 'viewConnections');
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requested = router.query.tab;
  const tab: TabKey = TABS.some((t) => t.key === requested) ? (requested as TabKey) : 'connections';

  const load = useCallback(async () => {
    setError(null);
    setData(null);
    try {
      const [connectors, connections, sources] = await Promise.all([
        warehouseService.listConnectors(),
        warehouseService.listConnections(),
        warehouseService.listSources(),
      ]);
      setData({ connectors, connections, sources });
    } catch (err) {
      setError(errorText(err, 'The warehouse settings could not be loaded.'));
    }
  }, []);

  useEffect(() => {
    if (allowed) void load();
  }, [allowed, load]);

  const select = (key: TabKey) => {
    void router.replace({ pathname: '/warehouse', query: key === 'connections' ? {} : { tab: key } }, undefined, {
      shallow: true,
    });
  };

  return (
    <div className="mx-auto max-w-6xl p-6">
      <PageTitle title="Warehouse" />
      <WarehouseHeader title="Warehouse" />
      <p className="mb-6 max-w-3xl text-sm text-slate-700">
        Analyse experiments with assignment and metric data that stays in your own warehouse. Controlvar sends a
        generated query and receives aggregates back; no rows are copied.
      </p>
      {!allowed ? (
        <RoleNotice message={refusal(user, 'viewConnections')} />
      ) : error ? (
        <LoadError message={error} onRetry={() => void load()} />
      ) : !data ? (
        <Loading label="Loading warehouse connections…" />
      ) : (
        <Tabs idPrefix="warehouse" label="Warehouse settings" tabs={TABS} selected={tab} onSelect={select}>
          {tab === 'connections' && <ConnectionsPanel data={data} />}
          {tab === 'assignment' && <SourcesPanel data={data} kind="assignment" />}
          {tab === 'metric' && <SourcesPanel data={data} kind="metric" />}
        </Tabs>
      )}
    </div>
  );
}

export default withModule(WarehousePage, WAREHOUSE_MODULE_NOTICE);
