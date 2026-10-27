import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useExperiment } from '@controlvar/react-sdk';
import ControlvarPanel, { PANEL_EVENT_LIMIT } from '@/components/ControlvarPanel';
import HomePage from '@/pages/index';
import { ASSIGNMENT_READ_MS } from '@/lib/assignments';
import { EventLogProvider, useTrack } from '@/lib/eventLog';
import { DASHBOARD_URL, EXPERIMENT_KEYS } from '@/lib/env';
import { __setExperiment, __setFlag, assignExperimentMock, DEFAULT_TEST_USER } from '@/__mocks__/controlvar-sdk';
import { resetTestState } from '@/test-utils';

function Emitter({ count }: { count: number }) {
  const track = useTrack();
  return (
    <button type="button" onClick={() => Array.from({ length: count }, (_, i) => track(`evt_${i + 1}`, { i: i + 1 }))}>
      emit
    </button>
  );
}

/** Stands in for a page that uses one experiment: assigns it on mount. */
function Uses({ experiment }: { experiment: string }) {
  useExperiment(experiment);
  return null;
}

function renderPanel(extra?: React.ReactNode, onNewVisitor = jest.fn()) {
  const utils = render(
    <EventLogProvider>
      {extra}
      <ControlvarPanel defaultOpen onNewVisitor={onNewVisitor} />
    </EventLogProvider>,
  );
  return { ...utils, onNewVisitor };
}

const assignedKeys = () => assignExperimentMock.mock.calls.map(([, key]) => key);

describe('ControlvarPanel', () => {
  beforeEach(resetTestState);
  afterEach(() => jest.useRealTimers());

  it('shows the visitor id and the variant of each experiment a page has assigned', () => {
    __setExperiment('shoplab_hero_banner', { variantName: 'video_hero', variantKey: 'video_hero', isControl: false });
    __setExperiment('shoplab_plp_sort', { variantName: 'ml_personalized', isControl: false });
    __setExperiment('shoplab_pdp_buy_button', { variantName: 'orange_buy_now', isControl: false });
    __setExperiment('shoplab_checkout_flow', { variantName: 'standard', isControl: true });
    renderPanel(Object.values(EXPERIMENT_KEYS).map((key) => <Uses key={key} experiment={key} />));

    expect(screen.getByTestId('panel-visitor-id')).toHaveTextContent(DEFAULT_TEST_USER.userId);
    expect(screen.getByTestId('panel-exp-shoplab_hero_banner')).toHaveTextContent('video_hero');
    expect(screen.getByTestId('panel-exp-shoplab_plp_sort')).toHaveTextContent('ml_personalized');
    expect(screen.getByTestId('panel-exp-shoplab_pdp_buy_button')).toHaveTextContent('orange_buy_now');
    expect(screen.getByTestId('panel-exp-shoplab_checkout_flow')).toHaveTextContent('standard');
    // An assigned row is the label, the key and the variant: no line under the key.
    expect(screen.queryByTestId('panel-exp-note-shoplab_hero_banner')).not.toBeInTheDocument();
  });

  it('assigns no experiment itself, and names the page that assigns each one', () => {
    renderPanel();

    expect(assignExperimentMock).not.toHaveBeenCalled();
    const pages: Record<string, string> = {
      shoplab_hero_banner: 'assigned when the home page opens',
      shoplab_plp_sort: 'assigned when the product list opens',
      shoplab_pdp_buy_button: 'assigned when a product page opens',
      shoplab_checkout_flow: 'assigned when the checkout page opens',
    };
    for (const [key, note] of Object.entries(pages)) {
      expect(screen.getByTestId(`panel-exp-${key}`)).toHaveTextContent('not assigned yet');
      expect(screen.getByTestId(`panel-exp-note-${key}`)).toHaveTextContent(note);
    }
  });

  it('a fresh visitor who opens only the home page is assigned to the hero test alone', () => {
    jest.useFakeTimers();
    __setExperiment('shoplab_hero_banner', { variantName: 'video_hero', variantKey: 'video_hero', isControl: false });
    renderPanel(<HomePage />);
    act(() => {
      jest.advanceTimersByTime(ASSIGNMENT_READ_MS);
    });

    expect(assignedKeys()).toEqual(['shoplab_hero_banner']);
    expect(screen.getByTestId('panel-exp-shoplab_hero_banner')).toHaveTextContent('video_hero');
    for (const key of ['shoplab_plp_sort', 'shoplab_pdp_buy_button', 'shoplab_checkout_flow']) {
      expect(screen.getByTestId(`panel-exp-${key}`)).toHaveTextContent('not assigned yet');
    }
  });

  it('shows an experiment a page assigns while the panel is open', () => {
    jest.useFakeTimers();
    __setExperiment('shoplab_plp_sort', { variantName: 'price_low_high', isControl: false });
    const { rerender } = renderPanel();
    expect(screen.getByTestId('panel-exp-shoplab_plp_sort')).toHaveTextContent('not assigned yet');

    rerender(
      <EventLogProvider>
        <Uses experiment="shoplab_plp_sort" />
        <ControlvarPanel defaultOpen onNewVisitor={jest.fn()} />
      </EventLogProvider>,
    );
    act(() => {
      jest.advanceTimersByTime(ASSIGNMENT_READ_MS);
    });
    expect(screen.getByTestId('panel-exp-shoplab_plp_sort')).toHaveTextContent('price_low_high');
  });

  it('says why a visitor the API did not enrol sees control', () => {
    __setExperiment('shoplab_hero_banner', { variantName: 'control', assigned: false, reason: 'paused' });
    __setExperiment('shoplab_plp_sort', { variantName: 'relevance', assigned: false, reason: 'completed' });
    __setExperiment('shoplab_pdp_buy_button', { variantName: 'control', assigned: false, reason: 'holdout' });
    renderPanel(['shoplab_hero_banner', 'shoplab_plp_sort', 'shoplab_pdp_buy_button'].map((key) => <Uses key={key} experiment={key} />));

    expect(screen.getByTestId('panel-exp-shoplab_hero_banner')).toHaveTextContent('not enrolled');
    expect(screen.getByTestId('panel-exp-note-shoplab_hero_banner')).toHaveTextContent('not enrolled: paused (control shown)');
    expect(screen.getByTestId('panel-exp-shoplab_plp_sort')).toHaveTextContent('completed');
    expect(screen.getByTestId('panel-exp-note-shoplab_plp_sort')).toHaveTextContent('experiment completed (control shown)');
    expect(screen.getByTestId('panel-exp-note-shoplab_pdp_buy_button')).toHaveTextContent('in the global holdout (control shown)');
    expect(document.body.textContent).not.toMatch(/\berror\b/);
  });

  it('shows flags as on/off and links to the dashboard', () => {
    __setFlag('shoplab_new_search', { isEnabled: true, variant: 'on' });
    __setFlag('shoplab_free_shipping_banner', { isEnabled: false });
    renderPanel();

    expect(screen.getByTestId('panel-flag-shoplab_new_search')).toHaveTextContent('on');
    expect(screen.getByTestId('panel-flag-shoplab_free_shipping_banner')).toHaveTextContent('off');
    expect(screen.getByRole('link', { name: /open dashboard/i })).toHaveAttribute('href', `${DASHBOARD_URL}/experiments`);
  });

  it(`lists only the last ${PANEL_EVENT_LIMIT} tracked events, newest first`, () => {
    renderPanel(<Emitter count={10} />);
    expect(screen.getByText('No events yet.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'emit' }));
    const items = within(screen.getByTestId('panel-events')).getAllByRole('listitem');
    expect(items).toHaveLength(PANEL_EVENT_LIMIT);
    expect(items[0]).toHaveTextContent('evt_10');
    expect(items[PANEL_EVENT_LIMIT - 1]).toHaveTextContent('evt_3');
    expect(screen.queryByText(/evt_1\b/)).not.toBeInTheDocument();
  });

  it('collapses/expands and the "New visitor" button calls the handler', () => {
    const { onNewVisitor } = renderPanel();
    const toggle = screen.getByRole('button', { name: /powered by controlvar/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(screen.getByRole('button', { name: 'New visitor' }));
    expect(onNewVisitor).toHaveBeenCalledTimes(1);

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('panel-visitor-id')).not.toBeInTheDocument();
  });
});
