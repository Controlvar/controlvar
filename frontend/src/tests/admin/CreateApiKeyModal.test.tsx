import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { CreateApiKeyModal } from '@/components/admin/api-keys/CreateApiKeyModal';
import type { Role } from '@/services/api';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import {
  MCP_READ_SCOPE,
  MCP_READ_SCOPE_HELP,
  MCP_READ_SCOPE_LABEL,
} from '@/components/admin/api-keys/mcpReadScope';

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
  'Only users who can change feature flags can create a key with this scope; the ADMIN and DEVELOPER roles and superusers can.';

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
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);

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

  describe('the mcp:read option', () => {
    const mcpBox = () => screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL });
    const okFetch = () =>
      jest.fn().mockResolvedValue({
        ok: true,
        status: 201,
        json: async () => ({ id: 'k', name: 'n', key: 'eptk_x', prefix: 'eptk_x', created_at: '' }),
      });
    const submitWith = async (fetchMock: jest.Mock) => {
      fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'n' } });
      fireEvent.click(screen.getByTestId('create-api-key-submit'));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      return JSON.parse(fetchMock.mock.calls[0][1].body);
    };

    it('is the literal scope string the server expects', () => {
      expect(MCP_READ_SCOPE).toBe('mcp:read');
    });

    it('is an unchecked checkbox, labelled by a <label for> and described by the exported help text', () => {
      global.fetch = jest.fn();
      render(<CreateApiKeyModal {...defaultProps} />);
      const box = mcpBox();
      expect(box).not.toBeChecked();
      // Programmatic association: the label element points at the input's id.
      const label = screen.getByText(MCP_READ_SCOPE_LABEL);
      expect(label.tagName).toBe('LABEL');
      expect(label).toHaveAttribute('for', box.id);
      expect(box.id).not.toBe('');
      // The visible help text is the exported constant, and the input points at it.
      const help = screen.getByTestId('api-key-mcp-read-scope-help');
      expect(help).toBeVisible();
      expect(help.textContent).toBe(MCP_READ_SCOPE_HELP);
      expect(box).toHaveAttribute('aria-describedby', help.id);
      expect(box).toHaveAccessibleDescription(MCP_READ_SCOPE_HELP);
      expect(help.className).toContain('text-slate-600');
    });

    it('says what the key reads and that its holder reads everything its owner can', () => {
      expect(MCP_READ_SCOPE_HELP).toMatch(/flags/);
      expect(MCP_READ_SCOPE_HELP).toMatch(/experiments/);
      expect(MCP_READ_SCOPE_HELP).toMatch(/everything its owner can read/);
      // No install or connect steps.
      expect(MCP_READ_SCOPE_HELP).not.toMatch(/install|npm|connect to|config/i);
    });

    it('is keyboard operable: Space toggles it', async () => {
      global.fetch = jest.fn();
      const user = userEvent.setup();
      render(<CreateApiKeyModal {...defaultProps} />);
      mcpBox().focus();
      expect(mcpBox()).toHaveFocus();
      await user.keyboard(' ');
      expect(mcpBox()).toBeChecked();
    });

    it.each(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'] as Role[])('is enabled for %s', (role) => {
      mockAuth.mockReturnValue(signedInAs(role));
      global.fetch = jest.fn();
      render(<CreateApiKeyModal {...defaultProps} />);
      expect(mcpBox()).toBeEnabled();
    });

    it('sends scopes ["mcp:read"] when ticked', async () => {
      const fetchMock = okFetch();
      global.fetch = fetchMock;
      render(<CreateApiKeyModal {...defaultProps} />);
      fireEvent.click(mcpBox());
      expect(mcpBox()).toBeChecked();
      expect(await submitWith(fetchMock)).toEqual({ name: 'n', scopes: ['mcp:read'] });
    });

    it('sends no scopes when unticked, and not mcp:read after a tick and an untick', async () => {
      const fetchMock = okFetch();
      global.fetch = fetchMock;
      render(<CreateApiKeyModal {...defaultProps} />);
      fireEvent.click(mcpBox());
      fireEvent.click(mcpBox());
      expect(await submitWith(fetchMock)).toEqual({ name: 'n' });
    });

    it('sends both scopes when both are ticked', async () => {
      const fetchMock = okFetch();
      global.fetch = fetchMock;
      render(<CreateApiKeyModal {...defaultProps} />);
      fireEvent.click(screen.getByTestId('api-key-ruleset-scope'));
      fireEvent.click(mcpBox());
      expect(await submitWith(fetchMock)).toEqual({
        name: 'n',
        scopes: ['sdk:ruleset', 'mcp:read'],
      });
    });

    it('still sends mcp:read, and only it, for a role that cannot take sdk:ruleset', async () => {
      mockAuth.mockReturnValue(signedInAs('VIEWER'));
      const fetchMock = okFetch();
      global.fetch = fetchMock;
      render(<CreateApiKeyModal {...defaultProps} />);
      fireEvent.click(mcpBox());
      expect(await submitWith(fetchMock)).toEqual({ name: 'n', scopes: ['mcp:read'] });
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

describe('CreateApiKeyModal accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  // Violations the dialog is known to have. #1613 (the dialog had no accessible
  // name) is fixed, so the list is empty: any violation is new.
  const KNOWN_DIALOG_VIOLATIONS: string[] = [];

  // Selectors that are the mcp:read checkbox or its help text.
  const MCP_TARGETS = ['#api-key-mcp-read-scope', '#api-key-mcp-read-scope-help'];

  async function scan(container: Element) {
    const result = await axe.run(container, axeOptions);
    return result.violations;
  }

  /** Violations that are neither known (#1613) nor attributable to the checkbox: any here is new. */
  function newViolations(violations: axe.Result[]) {
    return violations.filter((v) => !KNOWN_DIALOG_VIOLATIONS.includes(v.id)).map((v) => v.id);
  }

  /** Violations that touch the mcp:read checkbox or its help text. */
  function attributableToMcpCheckbox(violations: axe.Result[]) {
    return violations.filter((v) =>
      v.nodes.some((n) => n.target.some((t) => MCP_TARGETS.includes(String(t)))),
    );
  }

  it('the open dialog has only the known violations, and none touches the mcp:read checkbox', async () => {
    global.fetch = jest.fn();
    const { container } = render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.getByRole('checkbox', { name: MCP_READ_SCOPE_LABEL })).toBeInTheDocument();
    const violations = await scan(container);
    expect(attributableToMcpCheckbox(violations).map((v) => v.id)).toStrictEqual([]);
    expect(newViolations(violations)).toStrictEqual([]);
    // The known list is exact.
    expect(violations.map((v) => v.id).sort()).toStrictEqual(KNOWN_DIALOG_VIOLATIONS);
  });

  it('the dialog is named by its heading', () => {
    global.fetch = jest.fn();
    render(<CreateApiKeyModal {...defaultProps} />);
    expect(screen.getByRole('dialog', { name: 'Create API Key' })).toBeInTheDocument();
  });

  it('the new-violation check fires on an unlabelled input beside the checkbox (positive control)', async () => {
    global.fetch = jest.fn();
    const { container } = render(<CreateApiKeyModal {...defaultProps} />);
    const stray = document.createElement('input');
    stray.type = 'checkbox';
    container.appendChild(stray);
    const violations = await scan(container);
    expect(newViolations(violations)).toContain('label');
  });

  it('the attribution check fires when the mcp:read checkbox loses its label (positive control)', async () => {
    global.fetch = jest.fn();
    const { container } = render(<CreateApiKeyModal {...defaultProps} />);
    container.querySelector('label[for="api-key-mcp-read-scope"]')?.removeAttribute('for');
    const violations = await scan(container);
    expect(attributableToMcpCheckbox(violations).map((v) => v.id)).toContain('label');
  });
});

describe('CreateApiKeyModal keyboard and screen-reader behaviour (#1613)', () => {
  const SECRET = 'plaintextkey-must-not-be-announced';

  function Harness() {
    const [open, setOpen] = React.useState(false);
    return (
      <>
        <button data-testid="opener" onClick={() => setOpen(true)}>
          New key
        </button>
        <CreateApiKeyModal isOpen={open} onClose={() => setOpen(false)} onSuccess={jest.fn()} />
      </>
    );
  }

  function mockCreate() {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: 'k1',
        name: 'K',
        key: SECRET,
        prefix: 'plaintex',
        created_at: '2024-06-15T10:00:00Z',
        expires_at: null,
      }),
    });
  }

  describe('a create that resolves after the dialog closed', () => {
    function setup() {
      let resolveJson: (v: unknown) => void = () => {};
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => new Promise((resolve) => { resolveJson = resolve; }),
      });
      const onSuccess = jest.fn();
      function Page() {
        const [open, setOpen] = React.useState(false);
        return (
          <>
            <button data-testid="opener" onClick={() => setOpen(true)}>New key</button>
            <CreateApiKeyModal isOpen={open} onClose={() => setOpen(false)} onSuccess={onSuccess} />
          </>
        );
      }
      render(<Page />);
      fireEvent.click(screen.getByTestId('opener'));
      fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'K' } });
      fireEvent.click(screen.getByTestId('create-api-key-submit'));
      const created = {
        id: 'k1', name: 'K', key: SECRET, prefix: 'plaintex', created_at: '2024-06-15T10:00:00Z',
      };
      return { onSuccess, resolveCreate: () => resolveJson(created) };
    }

    it.each(['Escape', 'Cancel'])('%s, then the create resolves: the next open is an empty form', async (how) => {
      const { onSuccess, resolveCreate } = setup();
      await waitFor(() => expect(global.fetch).toHaveBeenCalled());
      if (how === 'Escape') fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      else fireEvent.click(screen.getByTestId('modal-cancel-button'));
      expect(screen.queryByTestId('create-api-key-modal')).not.toBeInTheDocument();

      await act(async () => { resolveCreate(); });
      await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
      expect(onSuccess).toHaveBeenCalledWith(SECRET);

      fireEvent.click(screen.getByTestId('opener'));
      expect(screen.getByTestId('api-key-name-input')).toHaveValue('');
      expect(screen.queryByTestId('masked-key-value')).not.toBeInTheDocument();
      expect(screen.getByRole('dialog', { name: 'Create API Key' })).toBeInTheDocument();
      expect(document.body.textContent).not.toContain(SECRET);
      expect(document.body.textContent).not.toContain('plaintex');
    });

    it('resolving after a reopen does not put the key into the new dialog', async () => {
      const { onSuccess, resolveCreate } = setup();
      await waitFor(() => expect(global.fetch).toHaveBeenCalled());
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      fireEvent.click(screen.getByTestId('opener'));
      await act(async () => { resolveCreate(); });
      await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
      expect(screen.queryByTestId('masked-key-value')).not.toBeInTheDocument();
      expect(document.body.textContent).not.toContain('plaintex');
    });
  });

  it('moves focus to the name field on open', () => {
    global.fetch = jest.fn();
    render(<Harness />);
    const opener = screen.getByTestId('opener');
    opener.focus();
    fireEvent.click(opener);
    expect(screen.getByTestId('api-key-name-input')).toHaveFocus();
  });

  it('Escape closes the dialog', () => {
    global.fetch = jest.fn();
    render(<Harness />);
    screen.getByTestId('opener').focus();
    fireEvent.click(screen.getByTestId('opener'));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('create-api-key-modal')).not.toBeInTheDocument();
  });

  it('returns focus to the opener when it closes', () => {
    global.fetch = jest.fn();
    render(<Harness />);
    const opener = screen.getByTestId('opener');
    opener.focus();
    fireEvent.click(opener);
    expect(opener).not.toHaveFocus();
    fireEvent.click(screen.getByTestId('modal-cancel-button'));
    expect(opener).toHaveFocus();
  });

  it('keeps Tab inside the dialog', () => {
    global.fetch = jest.fn();
    render(<Harness />);
    screen.getByTestId('opener').focus();
    fireEvent.click(screen.getByTestId('opener'));
    screen.getByTestId('create-api-key-submit').focus();
    // Submit is disabled with an empty name, so the last focusable is Cancel.
    screen.getByTestId('modal-cancel-button').focus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' });
    expect(screen.getByTestId('api-key-name-input')).toHaveFocus();
  });

  it('announces "Key created" then "Key copied" in a polite live region, never the key', async () => {
    mockCreate();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: jest.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    render(<CreateApiKeyModal {...defaultProps} />);
    const live = screen.getByTestId('api-key-live-region');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveTextContent('');
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'K' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(live).toHaveTextContent('Key created'));
    fireEvent.click(screen.getByTestId('copy-key-button'));
    await waitFor(() => expect(live).toHaveTextContent('Key copied'));
    expect(live.textContent).not.toContain(SECRET);
  });

  it('moves focus to Done once the key is shown', async () => {
    mockCreate();
    render(<Harness />);
    screen.getByTestId('opener').focus();
    fireEvent.click(screen.getByTestId('opener'));
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'K' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(screen.getByTestId('masked-key-value')).toBeInTheDocument());
    expect(screen.getByTestId('modal-done-button')).toHaveFocus();
  });

  it('puts the plaintext key in no accessible name, description or live region', async () => {
    mockCreate();
    render(<CreateApiKeyModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('api-key-name-input'), { target: { value: 'K' } });
    fireEvent.click(screen.getByTestId('create-api-key-submit'));
    await waitFor(() => expect(screen.getByTestId('masked-key-value')).toBeInTheDocument());
    const dialog = screen.getByRole('dialog');
    const ids = (el: Element, attr: string) =>
      (el.getAttribute(attr) ?? '')
        .split(/\s+/)
        .filter(Boolean)
        .map((id) => document.getElementById(id)?.textContent ?? '');
    const all = [dialog, ...Array.from(dialog.querySelectorAll('*'))];
    for (const el of all) {
      for (const attr of ['aria-label', 'title', 'aria-valuetext', 'aria-roledescription']) {
        expect(el.getAttribute(attr) ?? '').not.toContain(SECRET);
      }
      expect(ids(el, 'aria-labelledby').join(' ')).not.toContain(SECRET);
      expect(ids(el, 'aria-describedby').join(' ')).not.toContain(SECRET);
      if (el.matches('[aria-live], [role="status"], [role="alert"], [role="log"]')) {
        expect(el.textContent ?? '').not.toContain(SECRET);
      }
    }
    expect(screen.getByRole('dialog', { name: 'API Key Created' })).toBeInTheDocument();
    expect(dialog.textContent).not.toContain(SECRET);
  });
});
