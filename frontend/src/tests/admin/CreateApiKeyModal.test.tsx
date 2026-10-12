import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CreateApiKeyModal } from '@/components/admin/api-keys/CreateApiKeyModal';
import type { Role } from '@/services/api';

// The modal reads the signed-in user to decide whether to offer sdk:ruleset.
const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

function signedInAs(role: Role, isSuperuser = false) {
  return {
    user: {
      id: 'u-1',
      email: 'u@example.test',
      username: 'u',
      full_name: null,
      role,
      is_superuser: isSuperuser,
      is_active: true,
      auth_provider: 'local',
    },
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  // The admin page is for superusers; that is the default here.
  mockAuth.mockReturnValue(signedInAs('ADMIN', true));
});

const ROLE_REASON =
  'Only users who can change feature flags (the ADMIN and DEVELOPER roles, or a superuser) can create a key with this scope.';

const HELP_TEXT =
  "For server-side SDKs that evaluate flags locally, and for a server that forwards error reports: reports sent with this key count toward automatic rollback. A key with this scope will be able to download every flag's targeting rules, so keep it on a server.";

const defaultProps = {
  isOpen: true,
  onClose: jest.fn(),
  onSuccess: jest.fn(),
};

describe('CreateApiKeyModal', () => {
  it('renders when isOpen=true', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} isOpen={true} />);
    expect(screen.getByTestId('create-api-key-modal')).toBeInTheDocument();
  });

  it('does not render when isOpen=false', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} isOpen={false} />);
    expect(screen.queryByTestId('create-api-key-modal')).not.toBeInTheDocument();
  });

  it('shows name input', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.getByTestId('api-key-name-input')).toBeInTheDocument();
  });

  it('offers no scope select: the old read/write/admin choice was never enforced (regression)', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.queryByTestId('api-key-scope-input')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByText(/permission scope/i)).not.toBeInTheDocument();
  });

  it('offers one unchecked checkbox for the sdk:ruleset scope, described by its warning before submit', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    const box = screen.getByRole('checkbox', {
      name: 'Server-side local evaluation (sdk:ruleset)',
    });
    expect(box).not.toBeChecked();
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);

    // The warning is visible before anything is submitted, and a screen
    // reader hears it with the control.
    const help = screen.getByTestId('api-key-ruleset-scope-help');
    expect(help).toBeVisible();
    expect(help).toHaveTextContent(
      HELP_TEXT,
    );
    expect(box).toHaveAccessibleDescription(
      HELP_TEXT,
    );
    // Contrast: slate-600 (#475569) on white is 7.6:1. slate-400 is about 2.6:1.
    expect(help.className).toContain('text-slate-600');
    expect(help.className).not.toMatch(/text-slate-(300|400)/);
  });

  describe('the sdk:ruleset option follows the roles that can change flags', () => {
    const box = () =>
      screen.getByRole('checkbox', { name: 'Server-side local evaluation (sdk:ruleset)' });

    it.each([
      ['ADMIN', false],
      ['DEVELOPER', false],
      ['VIEWER', true],
    ] as [Role, boolean][])('is enabled for %s (superuser: %s)', (role, su) => {
      mockAuth.mockReturnValue(signedInAs(role, su));
      global.fetch = jest.fn();
      render(<CreateApiKeyModal {...defaultProps} />);
      expect(box()).toBeEnabled();
      expect(screen.queryByTestId('api-key-ruleset-scope-reason')).not.toBeInTheDocument();
    });

    it.each(['ANALYST', 'VIEWER'] as Role[])(
      'is disabled for %s, with the reason in text, and never sent',
      async (role) => {
        mockAuth.mockReturnValue(signedInAs(role));
        const fetchMock = jest.fn().mockResolvedValue({
          ok: true,
          status: 201,
          json: async () => ({ id: 'k', name: 'n', key: 'eptk_x', prefix: 'eptk_x', created_at: '' }),
        });
        global.fetch = fetchMock;
        render(<CreateApiKeyModal {...defaultProps} />);

        expect(box()).toBeDisabled();
        expect(box()).not.toBeChecked();
        const reason = screen.getByTestId('api-key-ruleset-scope-reason');
        expect(reason).toBeVisible();
        expect(reason).toHaveTextContent(ROLE_REASON);
        expect(box()).toHaveAccessibleDescription(`${HELP_TEXT} ${ROLE_REASON}`);

        fireEvent.click(box());
        expect(box()).not.toBeChecked();
        fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'n' } });
        fireEvent.click(screen.getByTestId('create-api-key-submit'));
        await waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ name: 'n' });
      },
    );

    it('is disabled when nobody is signed in', () => {
      mockAuth.mockReturnValue(null);
      global.fetch = jest.fn();
      render(<CreateApiKeyModal {...defaultProps} />);
      expect(box()).toBeDisabled();
      expect(screen.getByTestId('api-key-ruleset-scope-reason')).toHaveTextContent(ROLE_REASON);
    });
  });

  it('submit disabled when name empty', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.getByTestId('create-api-key-submit')).toBeDisabled();
  });

  it('calls onSuccess with the new key on successful creation (mock fetch POST /api/v1/api-keys)', async () => {
    const createdKey = {
      id: 'new-key-id',
      name: 'Test Key',
      key: 'abcdefgh_very_long_secret_key_value',
      prefix: 'abcdefgh',
      created_at: '2024-06-15T10:00:00Z',
      expires_at: null,
    };
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => createdKey,
    });
    global.fetch = fetchMock;

    const onSuccess = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} onSuccess={onSuccess} />);

    fireEvent.change(screen.getByTestId('api-key-name-input'), {
      target: { value: 'Test Key' },
    });

    fireEvent.click(screen.getByTestId('create-api-key-submit'));

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(createdKey.key);
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/api-keys');
    expect(init.method).toBe('POST');
    // Unchecked: the key carries no scope at all.
    expect(JSON.parse(init.body)).toEqual({ name: 'Test Key' });
  });

  it('sends scopes ["sdk:ruleset"] when the checkbox is ticked', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({ id: 'k', name: 'Server', key: 'eptk_x', prefix: 'eptk_x', created_at: '' }),
    });
    global.fetch = fetchMock;
    render(<CreateApiKeyModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'Server' } });
    fireEvent.click(screen.getByTestId('api-key-ruleset-scope'));
    expect(screen.getByTestId('api-key-ruleset-scope')).toBeChecked();
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      name: 'Server',
      scopes: ['sdk:ruleset'],
    });
  });

  it('still accepts the legacy key_value field', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 'k', name: 'Legacy', key_value: 'legacy_secret', prefix: 'legacy_s', created_at: '' }),
    });
    const onSuccess = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} onSuccess={onSuccess} />);
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'Legacy' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith('legacy_secret'));
  });

  it('shows the API error detail when creation fails', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      text: async () => JSON.stringify({ detail: 'Key name already exists' }),
    });
    render(<CreateApiKeyModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'Dup' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('modal-error')).toHaveTextContent('Key name already exists');
    });
  });

  it('shows created key value (masked to first 8 chars + "...") after creation', async () => {
    const createdKey = {
      id: 'new-key-id',
      name: 'Test Key',
      key: 'abcdefghXXXXXXXXXXXXXXXXXXXX',
      prefix: 'abcdefgh',
      created_at: '2024-06-15T10:00:00Z',
      expires_at: null,
    };
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => createdKey,
    });

    render(<CreateApiKeyModal {...defaultProps} />);

    fireEvent.change(screen.getByTestId('api-key-name-input'), {
      target: { value: 'Test Key' },
    });

    fireEvent.click(screen.getByTestId('create-api-key-submit'));

    await waitFor(() => {
      const maskedKey = screen.getByTestId('masked-key-value');
      expect(maskedKey).toBeInTheDocument();
      // Should show first 8 chars + "..."
      expect(maskedKey.textContent).toContain('abcdefgh');
      expect(maskedKey.textContent).toContain('...');
    });
  });

  it('renders with data-testid="create-api-key-modal"', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.getByTestId('create-api-key-modal')).toBeInTheDocument();
  });
});
