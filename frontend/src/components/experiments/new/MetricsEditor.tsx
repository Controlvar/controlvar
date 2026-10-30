import React from 'react';
import { MetricType, METRIC_TYPE_LABELS } from '@/types/experiments';
import { ExperimentFormAction, MetricFormData } from './formState';
import { smallInputClass } from './fieldStyles';

const METRIC_TYPES: Array<{ value: MetricType; label: string }> = (
  Object.keys(METRIC_TYPE_LABELS) as MetricType[]
).map((value) => ({ value, label: METRIC_TYPE_LABELS[value] }));

interface MetricsEditorProps {
  metrics: MetricFormData[];
  dispatch: React.Dispatch<ExperimentFormAction>;
  /**
   * The field a refusal shown elsewhere is about, and that refusal's element
   * id. The field then carries `aria-describedby` pointing at it, and a text
   * field `aria-invalid` too. Unset in guided setup, which renders as before.
   */
  invalid?: { index: number; field: 'name' | 'event_name' | 'is_primary' } | null;
  invalidDescribedBy?: string;
}

/** The metrics heading and one row per metric. The caller supplies the section. */
export function MetricsEditor({ metrics, dispatch, invalid = null, invalidDescribedBy }: MetricsEditorProps) {
  // A radio takes no aria-invalid; it is described by the refusal only.
  const marks = (index: number, field: 'name' | 'event_name' | 'is_primary') =>
    invalid && invalidDescribedBy && invalid.index === index && invalid.field === field
      ? {
          ...(field === 'is_primary' ? {} : { 'aria-invalid': true as const }),
          'aria-describedby': invalidDescribedBy,
        }
      : {};
  return (
    <>
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Metrics</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Conversions are matched by <span className="font-mono">event_name</span> from{' '}
            <span className="font-mono">/tracking/track</span>. One metric is primary.
          </p>
        </div>
        <button
          type="button"
          onClick={() => dispatch({ type: 'addMetric' })}
          className="text-sm text-blue-600 hover:text-blue-800 font-medium"
          data-testid="add-metric"
        >
          + Add Metric
        </button>
      </div>

      <div className="space-y-3">
        {metrics.map((metric, index) => (
          <div
            key={index}
            className="flex gap-3 items-start p-3 rounded-lg border border-slate-200 bg-slate-50"
            data-testid={`metric-row-${index}`}
          >
            <div className="flex-1 space-y-2">
              <div className="flex flex-col sm:flex-row gap-2">
                <input
                  type="text"
                  name={`metric_name_${index}`}
                  value={metric.name}
                  onChange={(e) =>
                    dispatch({ type: 'changeMetric', index, field: 'name', value: e.target.value })
                  }
                  placeholder="Metric name"
                  aria-label={`Metric ${index + 1} name`}
                  className={`flex-1 ${smallInputClass}`}
                  data-testid={`metric-name-${index}`}
                  {...marks(index, 'name')}
                />
                <input
                  type="text"
                  name={`metric_event_${index}`}
                  value={metric.event_name}
                  onChange={(e) =>
                    dispatch({ type: 'changeMetric', index, field: 'event_name', value: e.target.value })
                  }
                  placeholder="event_name (e.g. purchase)"
                  aria-label={`Metric ${index + 1} event name`}
                  className={`flex-1 font-mono ${smallInputClass}`}
                  data-testid={`metric-event-${index}`}
                  {...marks(index, 'event_name')}
                />
                <select
                  name={`metric_type_${index}`}
                  value={metric.metric_type}
                  onChange={(e) =>
                    dispatch({ type: 'changeMetric', index, field: 'metric_type', value: e.target.value })
                  }
                  aria-label={`Metric ${index + 1} type`}
                  className={smallInputClass}
                  data-testid={`metric-type-${index}`}
                >
                  {METRIC_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <label className="inline-flex items-center gap-1.5 text-xs text-slate-600">
                  <input
                    type="radio"
                    name="primary_metric"
                    checked={metric.is_primary}
                    onChange={() => dispatch({ type: 'setPrimary', index })}
                    data-testid={`metric-primary-${index}`}
                    {...marks(index, 'is_primary')}
                  />
                  Primary metric
                </label>
                {/* A fall is the improvement for errors or uninstalls: the
                    winner and the recommendation read it from this. */}
                <label className="inline-flex items-center gap-1.5 text-xs text-slate-600">
                  <input
                    type="checkbox"
                    name={`metric_lower_is_better_${index}`}
                    checked={metric.lower_is_better === true}
                    onChange={(e) =>
                      dispatch({
                        type: 'changeMetric',
                        index,
                        field: 'lower_is_better',
                        value: e.target.checked,
                      })
                    }
                    data-testid={`metric-lower-is-better-${index}`}
                  />
                  Lower is better (for example errors or uninstalls)
                </label>
              </div>
            </div>
            {metrics.length > 1 && (
              <button
                type="button"
                onClick={() => dispatch({ type: 'removeMetric', index })}
                className="text-red-400 hover:text-red-600 text-sm mt-1"
                data-testid={`remove-metric-${index}`}
                aria-label="Remove metric"
              >
                &times;
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
