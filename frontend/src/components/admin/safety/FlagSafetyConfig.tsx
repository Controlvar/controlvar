import React from 'react';

interface FlagSafetyConfigProps {
  flagId: string;
  flagName: string;
  onClose: () => void;
}

export function FlagSafetyConfig({ flagId, flagName, onClose }: FlagSafetyConfigProps) {
  return (
    <div
      data-testid="flag-safety-config"
      className="bg-white border border-slate-200 rounded-lg p-6 flex flex-col gap-4"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-base font-semibold text-slate-800">
          Safety Config: {flagName}
        </h3>
        <button
          data-testid="flag-safety-config-close"
          onClick={onClose}
          className="text-slate-600 hover:text-slate-800 transition-colors text-sm"
          aria-label="Close"
        >
          Close
        </button>
      </div>

      <p className="text-sm text-slate-600">
        Override thresholds for flag{' '}
        <span className="font-mono font-medium text-slate-800">{flagName}</span> (ID:{' '}
        <span className="font-mono text-slate-500">{flagId}</span>).
      </p>

      <div className="bg-slate-50 border border-slate-200 rounded p-4 text-sm text-slate-500">
        Per-flag safety threshold overrides will be configurable here.
      </div>
    </div>
  );
}
