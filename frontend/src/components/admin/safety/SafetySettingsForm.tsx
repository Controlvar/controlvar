import React, { useEffect, useState } from 'react';
import { AdminService } from '@/services/admin';
import { SafetyMetricThreshold, SafetySettings, SafetySettingsUpdate } from '@/types/admin';

interface SafetySettingsFormProps {
  onSaved?: () => void;
}

/**
 * Editable view of `SafetySettings.default_metrics` for the two metrics the
 * backend can measure (`error_rate`, `latency`). Error-rate thresholds are a
 * 0–1 fraction on the API and edited here as a percentage.
 */
interface ThresholdForm {
  enable_automatic_rollbacks: boolean;
  error_rate_warning: string;
  error_rate_critical: string;
  latency_warning: string;
  latency_critical: string;
}

const EMPTY_FORM: ThresholdForm = {
  enable_automatic_rollbacks: false,
  error_rate_warning: '',
  error_rate_critical: '',
  latency_warning: '',
  latency_critical: '',
};

function fractionToPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return String(parseFloat((value * 100).toPrecision(12)));
}

function percentToFraction(value: string): number | null {
  if (value.trim() === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? parseFloat((n / 100).toPrecision(12)) : null;
}

function numberOrNull(value: string): number | null {
  if (value.trim() === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function millis(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value);
}

export function settingsToForm(settings: SafetySettings): ThresholdForm {
  const metrics = settings.default_metrics ?? {};
  return {
    enable_automatic_rollbacks: settings.enable_automatic_rollbacks,
    error_rate_warning: fractionToPercent(metrics.error_rate?.warning_threshold),
    error_rate_critical: fractionToPercent(metrics.error_rate?.critical_threshold),
    latency_warning: millis(metrics.latency?.warning_threshold),
    latency_critical: millis(metrics.latency?.critical_threshold),
  };
}

function threshold(
  warning: number | null,
  critical: number | null,
  existing?: SafetyMetricThreshold,
): SafetyMetricThreshold | null {
  if (warning === null && critical === null) return null;
  return {
    warning_threshold: warning,
    critical_threshold: critical,
    comparison_type: existing?.comparison_type ?? 'greater_than',
  };
}

/** Build the `POST /safety/settings` body, keeping any metrics this form does not edit. */
export function formToUpdate(form: ThresholdForm, current: SafetySettings | null): SafetySettingsUpdate {
  const existing = current?.default_metrics ?? {};
  const metrics: Record<string, SafetyMetricThreshold> = { ...existing };

  const errorRate = threshold(
    percentToFraction(form.error_rate_warning),
    percentToFraction(form.error_rate_critical),
    existing.error_rate,
  );
  if (errorRate) metrics.error_rate = errorRate;
  else delete metrics.error_rate;

  const latency = threshold(
    numberOrNull(form.latency_warning),
    numberOrNull(form.latency_critical),
    existing.latency,
  );
  if (latency) metrics.latency = latency;
  else delete metrics.latency;

  return {
    enable_automatic_rollbacks: form.enable_automatic_rollbacks,
    default_metrics: Object.keys(metrics).length > 0 ? metrics : null,
  };
}

export function SafetySettingsForm({ onSaved }: SafetySettingsFormProps) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [settings, setSettings] = useState<SafetySettings | null>(null);
  const [form, setForm] = useState<ThresholdForm>(EMPTY_FORM);

  useEffect(() => {
    setLoading(true);
    AdminService.getSafetySettings()
      .then((data) => {
        setSettings(data);
        setForm(settingsToForm(data));
        setLoading(false);
      })
      .catch((err: Error) => {
        setError(err.message || 'Failed to load safety settings');
        setLoading(false);
      });
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type, checked } = e.target;
    setForm((prev) => ({ ...prev, [name]: type === 'checkbox' ? checked : value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const saved = await AdminService.updateSafetySettings(formToUpdate(form, settings));
      setSettings(saved);
      setForm(settingsToForm(saved));
      setSuccess(true);
      onSaved?.();
    } catch (err) {
      setError((err as Error).message || 'Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div data-testid="safety-settings-form-loading" className="flex flex-col gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-10 bg-slate-100 rounded animate-pulse" />
        ))}
      </div>
    );
  }

  const inputClass =
    'border border-slate-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-600';

  return (
    <form
      data-testid="safety-settings-form"
      onSubmit={handleSubmit}
      className="bg-white border border-slate-200 rounded-lg p-6 flex flex-col gap-5"
    >
      <div>
        <h2 className="text-base font-semibold text-slate-800">Safety Settings</h2>
        <p className="text-xs text-slate-500 mt-1">
          Default thresholds apply to every flag without its own safety config. Leave a field
          empty to not monitor that level.
        </p>
      </div>

      {error && (
        <div
          data-testid="settings-error"
          className="bg-red-50 border border-red-200 rounded p-3 text-red-700 text-sm"
        >
          {error}
        </div>
      )}

      {success && (
        <div
          data-testid="settings-success"
          className="bg-green-50 border border-green-200 rounded p-3 text-green-700 text-sm"
        >
          Settings saved successfully.
        </div>
      )}

      <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
        <input
          data-testid="auto-rollback-checkbox"
          type="checkbox"
          name="enable_automatic_rollbacks"
          checked={form.enable_automatic_rollbacks}
          onChange={handleChange}
          className="h-4 w-4 rounded border-slate-300"
        />
        Enable automatic rollbacks
      </label>

      <fieldset className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <legend className="text-sm font-semibold text-slate-700 mb-2">Error rate (%)</legend>
        <div className="flex flex-col gap-1">
          <label htmlFor="error_rate_warning" className="text-sm text-slate-600">
            Warning threshold
          </label>
          <input
            id="error_rate_warning"
            data-testid="error-rate-warning-input"
            type="number"
            name="error_rate_warning"
            value={form.error_rate_warning}
            onChange={handleChange}
            min={0}
            max={100}
            step={0.1}
            placeholder="e.g. 2"
            className={inputClass}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="error_rate_critical" className="text-sm text-slate-600">
            Critical threshold (triggers rollback)
          </label>
          <input
            id="error_rate_critical"
            data-testid="error-rate-critical-input"
            type="number"
            name="error_rate_critical"
            value={form.error_rate_critical}
            onChange={handleChange}
            min={0}
            max={100}
            step={0.1}
            placeholder="e.g. 5"
            className={inputClass}
          />
        </div>
      </fieldset>

      <fieldset className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <legend className="text-sm font-semibold text-slate-700 mb-2">Latency (ms)</legend>
        <div className="flex flex-col gap-1">
          <label htmlFor="latency_warning" className="text-sm text-slate-600">
            Warning threshold
          </label>
          <input
            id="latency_warning"
            data-testid="latency-warning-input"
            type="number"
            name="latency_warning"
            value={form.latency_warning}
            onChange={handleChange}
            min={0}
            placeholder="e.g. 300"
            className={inputClass}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="latency_critical" className="text-sm text-slate-600">
            Critical threshold (triggers rollback)
          </label>
          <input
            id="latency_critical"
            data-testid="latency-critical-input"
            type="number"
            name="latency_critical"
            value={form.latency_critical}
            onChange={handleChange}
            min={0}
            placeholder="e.g. 500"
            className={inputClass}
          />
        </div>
      </fieldset>

      <div className="flex justify-end">
        <button
          data-testid="save-settings-button"
          type="submit"
          disabled={saving}
          className="bg-blue-600 text-white px-4 py-2 rounded text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {saving ? 'Saving...' : 'Save Settings'}
        </button>
      </div>
    </form>
  );
}
