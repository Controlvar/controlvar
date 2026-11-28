'use client';
import React, { useState } from 'react';
import { NotificationPreference } from '@/types/admin';

interface NotificationPrefsFormProps {
  prefs: NotificationPreference;
  onSave: (data: Partial<NotificationPreference>) => Promise<void>;
}

export function NotificationPrefsForm({ prefs, onSave }: NotificationPrefsFormProps) {
  const [form, setForm] = useState({
    notify_experiment_started: prefs.notify_experiment_started,
    notify_experiment_completed: prefs.notify_experiment_completed,
    notify_safety_rollback: prefs.notify_safety_rollback,
    notify_rollout_advanced: prefs.notify_rollout_advanced,
    slack_channel: prefs.slack_channel ?? '',
    email_override: prefs.email_override ?? '',
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const handleToggle = (key: keyof typeof form) => {
    setForm(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      await onSave(form);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form data-testid="notification-prefs-form" onSubmit={handleSubmit} className="space-y-4">
      <h3 className="font-semibold text-slate-700">Notification Preferences</h3>

      {([
        ['notify_experiment_started', 'Experiment Started'],
        ['notify_experiment_completed', 'Experiment Completed'],
        ['notify_safety_rollback', 'Safety Rollback Alerts'],
        ['notify_rollout_advanced', 'Rollout Stage Advanced'],
      ] as [keyof typeof form, string][]).map(([key, label]) => (
        <label key={key} className="flex items-center gap-3">
          <input
            type="checkbox"
            data-testid={`pref-${key}`}
            checked={!!form[key]}
            onChange={() => handleToggle(key)}
            className="h-4 w-4"
          />
          <span className="text-sm text-slate-700">{label}</span>
        </label>
      ))}

      <div>
        <label htmlFor="slack-channel-input" className="block text-sm font-medium text-slate-700">Slack Channel Override</label>
        <input
          id="slack-channel-input"
          data-testid="slack-channel-input"
          type="text"
          value={form.slack_channel}
          onChange={e => setForm(p => ({ ...p, slack_channel: e.target.value }))}
          placeholder="#my-channel"
          className="mt-1 block w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
        />
      </div>

      <div>
        <label htmlFor="email-override-input" className="block text-sm font-medium text-slate-700">Email Override</label>
        <input
          id="email-override-input"
          data-testid="email-override-input"
          type="email"
          value={form.email_override}
          onChange={e => setForm(p => ({ ...p, email_override: e.target.value }))}
          placeholder="me@example.com"
          className="mt-1 block w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
        />
      </div>

      <button
        data-testid="save-prefs-btn"
        type="submit"
        disabled={saving}
        className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm hover:bg-blue-700 disabled:opacity-50"
      >
        {saving ? 'Saving...' : saved ? 'Saved!' : 'Save Preferences'}
      </button>
    </form>
  );
}
