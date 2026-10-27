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
          content="A phone drawn with the React SDK: pick a device, see which flags and experiments it gets."
        />
      </Head>
      <Component {...pageProps} />
    </>
  );
}
