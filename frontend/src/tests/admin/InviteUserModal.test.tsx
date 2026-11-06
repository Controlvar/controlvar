import React, { useState } from 'react';
import { act, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
// The dialog makes `#__next` inert and portals beside it, so every render is
// mounted the way Next mounts the page (#369).
import { APP_ROOT_ID, renderInAppRoot as render } from '@/tests/helpers/renderInAppRoot';
import {
  InviteUserModal,
  generateTemporaryPassword,
  usernameFromEmail,
} from '@/components/admin/users/InviteUserModal';

// Mock global fetch — the modal goes through AdminService.createUser → apiFetch → fetch.
const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = 'http://localhost:8000';

beforeEach(() => {
  jest.clearAllMocks();
  process.env.NEXT_PUBLIC_API_URL = BASE;
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

function mockCreated(overrides: Record<string, unknown> = {}) {
  mockFetch.mockResolvedValueOnce({
    ok: true,
    status: 201,
    json: () =>
      Promise.resolve({
        id: 'new-user',
        username: 'new',
        email: 'new@example.com',
        full_name: null,
        is_active: true,
        is_superuser: false,
        role: 'DEVELOPER',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        ...overrides,
      }),
  } as Response);
}

describe('generateTemporaryPassword', () => {
  it('satisfies the UserCreate password rules', () => {
    for (let i = 0; i < 25; i++) {
      const pw = generateTemporaryPassword();
      expect(pw.length).toBeGreaterThanOrEqual(8);
      expect(pw).toMatch(/[A-Z]/);
      expect(pw).toMatch(/[a-z]/);
      expect(pw).toMatch(/[0-9]/);
    }
  });

  it('never returns fewer than 8 characters', () => {
    expect(generateTemporaryPassword(3).length).toBe(8);
  });
});

describe('usernameFromEmail', () => {
  it('uses the sanitised local part', () => {
    expect(usernameFromEmail('Jane.Doe+ops@example.com')).toBe('jane.doe-ops');
    expect(usernameFromEmail('--x--@example.com')).toBe('x');
    expect(usernameFromEmail('')).toBe('');
  });
});

describe('InviteUserModal', () => {
  const defaultProps = {
    isOpen: true,
    onClose: jest.fn(),
    onSuccess: jest.fn(),
  };

  it('renders modal when isOpen=true', () => {
    render(<InviteUserModal {...defaultProps} />);
    expect(screen.getByTestId('invite-user-modal')).toBeInTheDocument();
  });

  it('does not render when isOpen=false', () => {
    render(<InviteUserModal {...defaultProps} isOpen={false} />);
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
  });

  it('shows email, username, full name and role inputs', () => {
    render(<InviteUserModal {...defaultProps} />);
    expect(screen.getByTestId('invite-email-input')).toBeInTheDocument();
    expect(screen.getByTestId('invite-username-input')).toBeInTheDocument();
    expect(screen.getByTestId('invite-full-name-input')).toBeInTheDocument();
    expect(screen.getByTestId('invite-role-select')).toBeInTheDocument();
  });

  it('submit button disabled when email is empty', () => {
    render(<InviteUserModal {...defaultProps} />);
    expect(screen.getByTestId('invite-submit-button')).toBeDisabled();
  });

  it('derives the username from the email until it is edited', () => {
    render(<InviteUserModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'Jane.Doe@example.com' },
    });
    expect((screen.getByTestId('invite-username-input') as HTMLInputElement).value).toBe('jane.doe');

    fireEvent.change(screen.getByTestId('invite-username-input'), { target: { value: 'jdoe' } });
    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'jane@example.com' },
    });
    expect((screen.getByTestId('invite-username-input') as HTMLInputElement).value).toBe('jdoe');
  });

  it('disables submit while the username is shorter than 3 characters', () => {
    render(<InviteUserModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'ab@example.com' },
    });
    expect(screen.getByTestId('invite-submit-button')).toBeDisabled();
    fireEvent.change(screen.getByTestId('invite-username-input'), { target: { value: 'abc' } });
    expect(screen.getByTestId('invite-submit-button')).not.toBeDisabled();
  });

  it('POSTs a UserCreate body to /api/v1/users/ and shows the temporary password once', async () => {
    const onClose = jest.fn();
    const onSuccess = jest.fn();
    mockCreated();

    render(<InviteUserModal isOpen={true} onClose={onClose} onSuccess={onSuccess} />);

    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByTestId('invite-full-name-input'), {
      target: { value: 'New Person' },
    });
    fireEvent.change(screen.getByTestId('invite-role-select'), { target: { value: 'DEVELOPER' } });

    const submitBtn = screen.getByTestId('invite-submit-button');
    expect(submitBtn).not.toBeDisabled();
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(screen.getByTestId('invite-created')).toBeInTheDocument();
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/api/v1/users/`);
    expect(init.method).toBe('POST');
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      email: 'new@example.com',
      username: 'new',
      full_name: 'New Person',
      role: 'DEVELOPER',
      is_active: true,
      is_superuser: false,
    });
    expect(body.password).toMatch(/^(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9]).{8,}$/);

    // The generated password is what the admin sees.
    expect(screen.getByTestId('invite-temp-password')).toHaveTextContent(body.password);
    // The copy names the menu item that opens /account/password.
    const created = screen.getByTestId('invite-created');
    expect(created).toHaveTextContent('After signing in, they change it with Change password');
    expect(created).not.toHaveTextContent('no password form');
    expect(onSuccess).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('invite-done-button'));
    expect(onClose).toHaveBeenCalled();
  });

  it('marks ADMIN invitees as superusers', async () => {
    mockCreated({ role: 'ADMIN', is_superuser: true });
    render(<InviteUserModal {...defaultProps} />);
    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'root@example.com' },
    });
    fireEvent.change(screen.getByTestId('invite-role-select'), { target: { value: 'ADMIN' } });
    fireEvent.click(screen.getByTestId('invite-submit-button'));
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.role).toBe('ADMIN');
    expect(body.is_superuser).toBe(true);
  });

  it('shows the API error message when creation fails', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: () => Promise.resolve({ detail: 'Email already registered' }),
    } as Response);

    render(<InviteUserModal {...defaultProps} />);

    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'fail@example.com' },
    });
    fireEvent.click(screen.getByTestId('invite-submit-button'));

    await waitFor(() => {
      expect(screen.getByTestId('invite-error-message')).toHaveTextContent('Email already registered');
    });
    expect(screen.queryByTestId('invite-created')).not.toBeInTheDocument();
  });

  it('calls onClose when cancel button clicked', () => {
    const onClose = jest.fn();
    render(<InviteUserModal {...defaultProps} onClose={onClose} />);
    fireEvent.click(screen.getByTestId('invite-cancel-button'));
    expect(onClose).toHaveBeenCalled();
  });

  it('renders with data-testid="invite-user-modal"', () => {
    render(<InviteUserModal {...defaultProps} />);
    expect(screen.getByTestId('invite-user-modal')).toBeInTheDocument();
  });
});

/** A page with the real opener: the Invite User button, as the users page has. */
function OpenedFromButton({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Invite User
      </button>
      <InviteUserModal
        isOpen={open}
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
        onSuccess={jest.fn()}
      />
    </>
  );
}

describe('InviteUserModal on the shared Dialog (#369)', () => {
  const appRoot = () => document.getElementById(APP_ROOT_ID) as HTMLElement;
  const opener = () => screen.getByRole('button', { name: 'Invite User' });

  async function openIt(onClose?: () => void) {
    const user = userEvent.setup();
    render(<OpenedFromButton onClose={onClose} />);
    await user.click(opener());
    return user;
  }

  /** Fill the form and submit it; resolves once the temporary password is shown. */
  async function createUser(user: ReturnType<typeof userEvent.setup>) {
    mockCreated();
    await user.type(screen.getByTestId('invite-email-input'), 'new@example.com');
    await user.click(screen.getByTestId('invite-submit-button'));
    await screen.findByTestId('invite-created');
  }

  it('opens from its button as a modal dialog named "Invite User", with focus on the email field', async () => {
    await openIt();
    const dialog = screen.getByRole('dialog', { name: 'Invite User' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toBe(screen.getByTestId('invite-user-modal'));
    expect(screen.getByTestId('invite-email-input')).toHaveFocus();
  });

  it('the page behind it is inert while it is open, and not after', async () => {
    const user = await openIt();
    expect(appRoot()).toHaveAttribute('inert');
    expect(appRoot().contains(screen.getByTestId('invite-user-modal'))).toBe(false);
    await user.click(screen.getByTestId('invite-cancel-button'));
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('Escape closes it before the password is shown, once, and focus returns to the opener; so does Cancel', async () => {
    const onClose = jest.fn();
    const user = await openIt(onClose);
    await user.type(screen.getByTestId('invite-email-input'), 'half@example.com');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
    expect(opener()).toHaveFocus();

    // The next open is an empty form.
    await user.click(opener());
    expect(screen.getByTestId('invite-email-input')).toHaveValue('');
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(opener()).toHaveFocus();

    await user.click(opener());
    await user.click(screen.getByTestId('invite-cancel-button'));
    expect(onClose).toHaveBeenCalledTimes(3);
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
    expect(opener()).toHaveFocus();
  });

  it('Escape does nothing while the create is in flight, and closes it again once that fails', async () => {
    let fail: (response: unknown) => void = () => {};
    mockFetch.mockReturnValueOnce(
      new Promise((resolve) => {
        fail = resolve;
      }),
    );
    const onClose = jest.fn();
    const user = await openIt(onClose);
    await user.type(screen.getByTestId('invite-email-input'), 'slow@example.com');
    await user.click(screen.getByTestId('invite-submit-button'));
    expect(screen.getByTestId('invite-submit-button')).toHaveTextContent('Creating...');

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Invite User' })).toBeInTheDocument();

    await act(async () => {
      fail({
        ok: false,
        status: 409,
        statusText: 'Conflict',
        json: () => Promise.resolve({ detail: 'Email already registered' }),
      });
    });
    await screen.findByTestId('invite-error-message');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
  });

  it('Escape and backdrop do nothing once the password is shown; Done closes', async () => {
    const onClose = jest.fn();
    const user = await openIt(onClose);
    await createUser(user);
    const dialog = screen.getByRole('dialog', { name: 'User created' });
    const password = screen.getByTestId('invite-temp-password').textContent;
    expect(password).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape' });
    await user.keyboard('{Escape}');
    // The backdrop is the element the dialog sits in.
    const backdrop = dialog.parentElement as HTMLElement;
    expect(backdrop).not.toBe(document.body);
    await user.click(backdrop);
    fireEvent.click(backdrop);

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'User created' })).toBe(dialog);
    expect(screen.getByTestId('invite-temp-password')).toHaveTextContent(password as string);
    expect(appRoot()).toHaveAttribute('inert');

    await user.click(screen.getByTestId('invite-done-button'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('invite-user-modal')).not.toBeInTheDocument();
    expect(appRoot()).not.toHaveAttribute('inert');
    expect(opener()).toHaveFocus();
  });

  it('moves focus to the password\'s Copy button once the password is shown, and keeps Tab inside', async () => {
    const user = await openIt();
    await createUser(user);
    expect(screen.getByTestId('invite-copy-password-button')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('invite-done-button')).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId('invite-copy-password-button')).toHaveFocus();
  });

  it('Tab from Cancel wraps to the email field; the page behind is never reached', async () => {
    const user = await openIt();
    // Create User is disabled while the email is empty, so Cancel is the last control.
    screen.getByTestId('invite-cancel-button').focus();
    await user.tab();
    expect(screen.getByTestId('invite-email-input')).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByTestId('invite-cancel-button')).toHaveFocus();
  });

  it('a failed create shows its error inside the dialog as an alert, and the dialog stays open', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: () => Promise.resolve({ detail: 'Email already registered' }),
    } as Response);
    const user = await openIt();
    await user.type(screen.getByTestId('invite-email-input'), 'taken@example.com');
    await user.click(screen.getByTestId('invite-submit-button'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Email already registered');
    expect(screen.getByRole('dialog', { name: 'Invite User' })).toContainElement(alert);
    expect(appRoot()).toHaveAttribute('inert');
  });

  describe('axe-core on the dialog node (colour contrast is not computable in jsdom)', () => {
    const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

    async function dialogViolations() {
      // By role alone: a lost name must be axe's finding, not a failed query.
      const dialog = screen.getByRole('dialog');
      expect(document.body).toContainElement(dialog);
      const result = await axe.run(dialog, axeOptions);
      const violations = result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
      // A clean scan must have run the rule that names the dialog, on it.
      if (violations.length === 0) expect(result.passes.map((p) => p.id)).toContain('aria-dialog-name');
      return violations;
    }

    it('the form, with an error, has no axe violations', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 409,
        statusText: 'Conflict',
        json: () => Promise.resolve({ detail: 'Email already registered' }),
      } as Response);
      const user = await openIt();
      await user.type(screen.getByTestId('invite-email-input'), 'taken@example.com');
      await user.click(screen.getByTestId('invite-submit-button'));
      await screen.findByTestId('invite-error-message');
      expect(await dialogViolations()).toStrictEqual([]);
    });

    it('the temporary password view has no axe violations', async () => {
      const user = await openIt();
      await createUser(user);
      expect(await dialogViolations()).toStrictEqual([]);
    });
  });
});
