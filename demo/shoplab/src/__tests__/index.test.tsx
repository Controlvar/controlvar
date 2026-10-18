import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import HomePage from '@/pages/index';
import { __setExperiment, trackEventMock } from '@/__mocks__/controlvar-sdk';
import { mockRouter } from '@/__mocks__/next-router';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

describe('Home page hero (shoplab_hero_banner)', () => {
  beforeEach(resetTestState);

  it('renders the control headline and CTA from the variant configuration', () => {
    __setExperiment('shoplab_hero_banner', {
      variantName: 'control',
      isControl: true,
      configuration: { media: 'image', headline: 'Gear up for the season', cta: 'Shop the collection' },
    });
    renderPage(<HomePage />);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Gear up for the season');
    expect(screen.getByRole('button', { name: 'Shop the collection' })).toBeInTheDocument();
    expect(screen.getByTestId('hero')).toHaveAttribute('data-media', 'image');
    expect(screen.getByTestId('hero')).not.toHaveClass('hero-video');
  });

  it('renders the video variant with its own copy and the animated placeholder', () => {
    __setExperiment('shoplab_hero_banner', {
      variantName: 'video_hero',
      isControl: false,
      configuration: { media: 'video', headline: 'See it in motion', cta: 'Watch & shop' },
    });
    renderPage(<HomePage />);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('See it in motion');
    expect(screen.getByRole('button', { name: 'Watch & shop' })).toBeInTheDocument();
    expect(screen.getByTestId('hero')).toHaveAttribute('data-media', 'video');
    expect(screen.getByTestId('hero')).toHaveClass('hero-video');
  });

  it('falls back to defaults while the assignment is loading', () => {
    __setExperiment('shoplab_hero_banner', { loading: true, configuration: null });
    renderPage(<HomePage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Gear up for the season');
    expect(screen.getByRole('button', { name: 'Shop the collection' })).toBeInTheDocument();
  });

  it('fires page_view on mount and hero_cta_click + navigation on the CTA', () => {
    __setExperiment('shoplab_hero_banner', {
      variantName: 'video_hero',
      configuration: { media: 'video', headline: 'See it in motion', cta: 'Watch & shop' },
    });
    renderPage(<HomePage />);

    expect(callsFor('page_view')).toHaveLength(1);
    expect(callsFor('page_view')[0][1]).toEqual({ page: 'home' });

    fireEvent.click(screen.getByRole('button', { name: 'Watch & shop' }));
    const [call] = callsFor('hero_cta_click');
    expect(call[1]).toEqual({ variant: 'video_hero', media: 'video' });
    expect(call[2]).toBeUndefined(); // no explicit key → SDK fans out to every cached assignment
    expect(mockRouter.push).toHaveBeenCalledWith('/products');
    expect(trackEventMock).toHaveBeenCalledTimes(2);
  });
});
