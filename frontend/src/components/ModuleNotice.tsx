import React, { useCallback, useState } from 'react';
import { PageTitle } from '@/components/PageTitle';
import { useModules } from '@/contexts/ModulesContext';
import { MODULES_DOC_PATH } from '@/services/modules';

export interface ModuleNoticeProps {
  /** Human name of the thing that is not available, e.g. `Workspaces`. */
  title: string;
  /** Module name, e.g. `workspaces`. Shown so support can match it. */
  module: string;
  /** One line on what the module does, for someone who has never seen it. */
  description: string;
}

/**
 * What a module route renders when the module is not installed.
 *
 * It is a statement of fact plus a documentation link: the route exists, the
 * module is not in this deployment, here is where the modules are described.
 */
export function ModuleNotice({ title, module, description }: ModuleNoticeProps) {
  return (
    <div
      data-testid="module-notice"
      data-module={module}
      data-state="not-installed"
      className="p-6"
    >
      <PageTitle title={`${title} — module not installed`} />
      <div className="max-w-2xl mx-auto mt-12 rounded-lg border border-slate-200 bg-white p-8 text-center">
        <p className="text-xs font-semibold uppercase tracking-wider text-slate-600">Module</p>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">{title}</h1>
        <p className="mt-3 text-sm text-slate-600">{description}</p>
        <p className="mt-4 text-sm text-slate-500">
          The <strong>{title}</strong> module is not installed in this deployment. Modules are part
          of the full profile; see the{' '}
          <a href={MODULES_DOC_PATH} target="_blank" rel="noreferrer" className="font-medium text-blue-700 underline">
            modules guide
          </a>
          .
        </p>
      </div>
    </div>
  );
}

export interface ModuleUnavailableNoticeProps extends ModuleNoticeProps {
  /** Re-run the probe. */
  onRetry: () => void;
  /** True while a retry is in flight. */
  retrying?: boolean;
}

/**
 * What a module route renders when the dashboard could not *ask* which modules
 * are installed.
 *
 * This is a different fact from {@link ModuleNotice} and must read like one.
 * One 502, or a probe that hits the ten-second timeout, resolves the context to
 * the core profile with `error` set — so a page gated on a module would
 * otherwise announce "the Workspaces module is not installed in this
 * deployment" on an instance where it plainly is, and the retry back-off
 * (30 s, 60 s, ... 300 s) could leave that on screen for minutes. Here the copy
 * says what actually happened and offers the retry directly.
 */
export function ModuleUnavailableNotice({
  title,
  module,
  description,
  onRetry,
  retrying = false,
}: ModuleUnavailableNoticeProps) {
  return (
    <div
      data-testid="module-notice"
      data-module={module}
      data-state="unreachable"
      className="p-6"
    >
      <PageTitle title={`${title} — unavailable`} />
      <div className="max-w-2xl mx-auto mt-12 rounded-lg border border-slate-200 bg-white p-8 text-center">
        <p className="text-xs font-semibold uppercase tracking-wider text-slate-600">Module</p>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">{title}</h1>
        <p className="mt-3 text-sm text-slate-600">{description}</p>
        <p className="mt-4 text-sm text-slate-500">
          The dashboard could not reach the API to check whether the{' '}
          <strong>{title}</strong> module is installed, so this page cannot be shown. This is not a
          statement that the module is missing — the check itself failed.
        </p>
        <button
          type="button"
          data-testid="module-notice-retry"
          onClick={onRetry}
          disabled={retrying}
          className="mt-5 inline-flex items-center rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          {retrying ? 'Checking…' : 'Try again'}
        </button>
      </div>
    </div>
  );
}

/**
 * Build the page component a module route falls back to. Used by every
 * module under `src/modules-stub/pages/`, which is what `@modules/pages/*`
 * resolves to in a core build.
 *
 * Always the "not installed" notice: the stub tree only exists in a core
 * bundle, where the module code is not there to run whatever the probe says.
 */
export function modulePageStub(props: ModuleNoticeProps): React.FC {
  const Stub: React.FC = () => <ModuleNotice {...props} />;
  Stub.displayName = `ModuleStub(${props.title})`;
  return Stub;
}

/**
 * Gate a real module page on the module being installed.
 *
 * The sidebar only hides the link; a bookmarked or typed URL still reaches the
 * page, whose first API call is then refused with 501. Wrapped, the page
 * renders one of three things:
 *
 * * nothing at all while the modules are still being probed — an installed
 *   page must not flash a notice before the answer arrives;
 * * {@link ModuleUnavailableNotice} when the probe *failed*, because "we could
 *   not ask" is not "it is not installed";
 * * {@link ModuleNotice} when the answer came back and the module is not in it.
 */
export function withModule<P extends object>(
  Page: React.ComponentType<P>,
  notice: ModuleNoticeProps,
): React.FC<P> {
  const Gated: React.FC<P> = (props) => {
    const { isLoading, error, hasModule, refresh } = useModules();
    const [retrying, setRetrying] = useState(false);
    const retry = useCallback(() => {
      setRetrying(true);
      void refresh().finally(() => setRetrying(false));
    }, [refresh]);

    // `isLoading` is "no answer yet", which is only ever the first probe —
    // `refresh()` deliberately leaves it alone (see ModulesContext). That is
    // what keeps the notice, and the button the user just clicked, mounted
    // while a retry is in flight: `error` is still set, so this renders the
    // unreachable notice with `retrying` until the probe answers.
    if (isLoading) return null;
    if (error) {
      return <ModuleUnavailableNotice {...notice} onRetry={retry} retrying={retrying} />;
    }
    if (!hasModule(notice.module)) return <ModuleNotice {...notice} />;
    return <Page {...props} />;
  };
  Gated.displayName = `withModule(${Page.displayName ?? Page.name ?? 'Page'})`;
  return Gated;
}

export default ModuleNotice;
