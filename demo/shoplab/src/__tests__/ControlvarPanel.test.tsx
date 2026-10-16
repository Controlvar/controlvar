import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import ControlvarPanel, { PANEL_EVENT_LIMIT } from '@/components/ControlvarPanel';
import { EventLogProvider, useTrack } from '@/lib/eventLog';
import { DASHBOARD_URL } from '@/lib/env';
import { __setExperiment, __setFlag, DEFAULT_TEST_USER } from '@/__mocks__/experimently-sdk';
import { resetTestState } from '@/test-utils';

function Emitter({ count }: { count: number }) {
  const track = useTrack();
  return (
    <button type="button" onClick={() => Array.from({ length: count }, (_, i) => track(`evt_${i + 1}`, { i: i + 1 }))}>
      emit
    </button>
  );
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

describe('ControlvarPanel', () => {
  beforeEach(resetTestState);

  it('shows the visitor id and the variant for each of the four experiments', () => {
    __setExperiment('shoplab_hero_banner', { variantName: 'video_hero', variantKey: 'video_hero', isControl: false });
    __setExperiment('shoplab_plp_sort', { variantName: 'ml_personalized', isControl: false });
    __setExperiment('shoplab_pdp_buy_button', { variantName: 'orange_buy_now', isControl: false });
    __setExperiment('shoplab_checkout_flow', { variantName: 'standard', isControl: true });
    renderPanel();

    expect(screen.getByTestId('panel-visitor-id')).toHaveTextContent(DEFAULT_TEST_USER.userId);
    expect(screen.getByTestId('panel-exp-shoplab_hero_banner')).toHaveTextContent('video_hero');
    expect(screen.getByTestId('panel-exp-shoplab_plp_sort')).toHaveTextContent('ml_personalized');
    expect(screen.getByTestId('panel-exp-shoplab_pdp_buy_button')).toHaveTextContent('orange_buy_now');
    expect(screen.getByTestId('panel-exp-shoplab_checkout_flow')).toHaveTextContent('standard');
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
