import '@/styles/globals.css';
import React from 'react';
import Head from 'next/head';
import type { AppProps } from 'next/app';

export default function StreamPulseNextApp({ Component, pageProps }: AppProps) {
  return (
    <>
      <Head>
        <title>StreamPulse — feature flags on a simulated phone</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta
          name="description"
          content="StreamPulse is a demo mobile streaming app showing feature-flag rollouts, targeting and experiments on Controlvar."
        />
      </Head>
      <Component {...pageProps} />
    </>
  );
}
