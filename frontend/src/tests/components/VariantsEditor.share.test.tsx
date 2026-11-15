/**
 * A version's traffic share box on the new-experiment form and the draft
 * design edit (#1428).
 *
 * Before, an emptied box held 0 and the number input showed "0" in its place.
 * The browser leaves the caret at the end of the box, so the digits typed
 * next landed after the 0: emptying the box and typing 34 showed "034".
 *
 * The keys are typed as the browser types them with the caret at the end:
 * each one appended to what the box shows. user-event keeps a caret of its
 * own, which does not follow React's write of "0", so it types "34" either
 * way and cannot show the bug.
 */
import React, { useReducer } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { VariantsEditor } from '@/components/experiments/new/VariantsEditor';
import { createInitialFormState, experimentFormReducer } from '@/components/experiments/new/formState';

function Harness() {
  const [state, dispatch] = useReducer(experimentFormReducer, undefined, createInitialFormState);
  return <VariantsEditor variants={state.variants} dispatch={dispatch} />;
}

it('an emptied share box stays empty while typed in, so typing 34 shows 34, not 034 (#1428)', () => {
  render(<Harness />);
  const box = screen.getByTestId('variant-allocation-0') as HTMLInputElement;
  const empty = () => fireEvent.change(box, { target: { value: '' } });
  const typeAtEnd = (keys: string) => {
    for (const key of keys) fireEvent.change(box, { target: { value: box.value + key } });
  };
  expect(box.value).toBe('50');

  box.focus();
  empty();
  typeAtEnd('34');
  expect(box.value).toBe('34');
  // The form holds the number typed: 34 here plus the other version's 50.
  expect(screen.getByTestId('allocation-total')).toHaveTextContent('Currently 84%');

  empty();
  expect(box.value).toBe('');

  // Leaving the box shows the number the form holds: 0 for an emptied box.
  fireEvent.blur(box);
  expect(box.value).toBe('0');
  expect(screen.getByTestId('allocation-total')).toHaveTextContent('Currently 50%');
});
