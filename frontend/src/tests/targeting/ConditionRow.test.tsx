import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConditionRow } from '@/components/targeting/ConditionRow';
import { TargetingCondition } from '@/types/targeting';
import { FLAG_OPERATOR_OPTIONS, OperatorOptions } from '@/utils/targeting';

const defaultCondition: TargetingCondition = {
  id: 'cond-1',
  attribute: 'user.country',
  operator: 'equals',
  value: 'US',
};

const emptyCondition: TargetingCondition = {
  id: 'cond-2',
  attribute: '',
  operator: 'equals',
  value: '',
};

describe('ConditionRow', () => {
  it('renders attribute input, operator select, and value input', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    expect(screen.getByTestId('condition-attribute')).toBeInTheDocument();
    expect(screen.getByTestId('condition-operator')).toBeInTheDocument();
    expect(screen.getByTestId('condition-value')).toBeInTheDocument();
  });

  it('renders remove button', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    expect(screen.getByTestId('condition-remove')).toBeInTheDocument();
  });

  it('calls onRemove when remove button is clicked', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    fireEvent.click(screen.getByTestId('condition-remove'));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('calls onChange with updated condition when attribute changes', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const attributeInput = screen.getByTestId('condition-attribute');
    fireEvent.change(attributeInput, { target: { value: 'user.plan' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated = onChange.mock.calls[0][0] as TargetingCondition;
    expect(updated.attribute).toBe('user.plan');
  });

  it('calls onChange with updated condition when operator changes', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const operatorSelect = screen.getByTestId('condition-operator');
    fireEvent.change(operatorSelect, { target: { value: 'contains' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated = onChange.mock.calls[0][0] as TargetingCondition;
    expect(updated.operator).toBe('contains');
  });

  it('calls onChange with updated condition when value changes', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const valueInput = screen.getByTestId('condition-value');
    fireEvent.change(valueInput, { target: { value: 'UK' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated = onChange.mock.calls[0][0] as TargetingCondition;
    expect(updated.value).toBe('UK');
  });

  it('hides value input for is_null operator', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    const condition: TargetingCondition = {
      ...defaultCondition,
      operator: 'is_null',
      value: null,
    };
    render(
      <ConditionRow
        condition={condition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    expect(screen.queryByTestId('condition-value')).not.toBeInTheDocument();
  });

  it('hides value input for is_not_null operator', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    const condition: TargetingCondition = {
      ...defaultCondition,
      operator: 'is_not_null',
      value: null,
    };
    render(
      <ConditionRow
        condition={condition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    expect(screen.queryByTestId('condition-value')).not.toBeInTheDocument();
  });

  it('shows comma-hint placeholder for in operator', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    const condition: TargetingCondition = {
      ...defaultCondition,
      operator: 'in',
      value: '',
    };
    render(
      <ConditionRow
        condition={condition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const valueInput = screen.getByTestId('condition-value');
    expect(valueInput).toHaveAttribute('placeholder', expect.stringContaining(','));
  });

  it('shows comma-hint placeholder for not_in operator', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    const condition: TargetingCondition = {
      ...defaultCondition,
      operator: 'not_in',
      value: '',
    };
    render(
      <ConditionRow
        condition={condition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const valueInput = screen.getByTestId('condition-value');
    expect(valueInput).toHaveAttribute('placeholder', expect.stringContaining(','));
  });

  it('changes operator options when attribute changes from string to semver', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    // Start with app.version (semver type)
    const semverCondition: TargetingCondition = {
      id: 'cond-s',
      attribute: 'app.version',
      operator: 'semver_eq',
      value: '1.0.0',
    };
    render(
      <ConditionRow
        condition={semverCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    const operatorSelect = screen.getByTestId('condition-operator');
    // Should have semver options
    const options = Array.from(operatorSelect.querySelectorAll('option')).map(
      (o) => (o as HTMLOptionElement).value
    );
    expect(options).toContain('semver_eq');
    expect(options).toContain('semver_gt');
    // Should not have string-only options
    expect(options).not.toContain('contains');
  });

  it('shows datalist for attribute autocomplete suggestions', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={emptyCondition}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
    // A datalist element should be present for autocomplete
    const datalist = document.querySelector('datalist');
    expect(datalist).toBeInTheDocument();
  });

  describe('picking an attribute whose suggested type is a version (#1775)', () => {
    const pick = (condition: TargetingCondition, attribute: string, operatorOptions?: OperatorOptions) => {
      const onChange = jest.fn();
      const { unmount } = render(
        <ConditionRow condition={condition} onChange={onChange} onRemove={jest.fn()} operatorOptions={operatorOptions} />,
      );
      fireEvent.change(screen.getByTestId('condition-attribute'), { target: { value: attribute } });
      unmount();
      return onChange.mock.calls[0][0] as TargetingCondition;
    };
    const versionCondition: TargetingCondition = { id: 'cond-v', attribute: 'app_version', operator: 'semver_eq', value: '2.3.0' };

    it.each([
      ['experiment', undefined],
      ['flag', FLAG_OPERATOR_OPTIONS],
    ])('picking App Version on a new condition selects version equals, as app.version did (%s page)', (_page, options) => {
      expect(pick(emptyCondition, 'app_version', options).operator).toBe('semver_eq');
      expect(pick(emptyCondition, 'app.version', options).operator).toBe('semver_eq');
    });

    it('keeps a version operator already chosen', () => {
      const chosen: TargetingCondition = { ...versionCondition, attribute: 'app.version', operator: 'semver_gte' };
      expect(pick(chosen, 'app_version').operator).toBe('semver_gte');
    });

    it('picking a text attribute after it behaves as before: the first text operator, or a version operator the page offers there', () => {
      expect(pick(versionCondition, 'user.country')).toEqual({ ...versionCondition, attribute: 'user.country', operator: 'equals' });
      expect(pick(versionCondition, 'device_type').operator).toBe('equals');
      expect(pick(versionCondition, 'os_version', FLAG_OPERATOR_OPTIONS).operator).toBe('semver_eq');
    });

    it('keeps the operator for attributes that are not versions', () => {
      expect(pick(emptyCondition, 'device_type').operator).toBe('equals');
      expect(pick(emptyCondition, 'account_age_days').operator).toBe('equals');
      expect(pick({ ...emptyCondition, operator: 'contains' }, 'account_age_days').operator).toBe('contains');
      expect(pick(emptyCondition, 'os_version', FLAG_OPERATOR_OPTIONS).operator).toBe('equals');
    });
  });

  it('suggests the names the docs tell an app to send, not the former ones (#1775)', () => {
    render(<ConditionRow condition={emptyCondition} onChange={jest.fn()} onRemove={jest.fn()} />);
    const offered = Array.from(document.querySelectorAll('datalist option')).map((o) => o.getAttribute('value'));
    expect(offered).toEqual([
      'user.country',
      'user.plan',
      'account_age_days',
      'user.email',
      'device_type',
      'app_version',
      'user.tags',
      'segment',
    ]);
    for (const former of ['device.type', 'app.version', 'user.age', 'session.new_user']) {
      expect(offered).not.toContain(former);
    }
  });

  it('disables all inputs when readOnly is true', () => {
    const onChange = jest.fn();
    const onRemove = jest.fn();
    render(
      <ConditionRow
        condition={defaultCondition}
        onChange={onChange}
        onRemove={onRemove}
        readOnly
      />
    );
    expect(screen.getByTestId('condition-attribute')).toBeDisabled();
    expect(screen.getByTestId('condition-operator')).toBeDisabled();
    expect(screen.getByTestId('condition-value')).toBeDisabled();
    expect(screen.queryByTestId('condition-remove')).not.toBeInTheDocument();
  });
});
