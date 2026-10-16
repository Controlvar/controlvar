import '@/styles/globals.css';
import React, { useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import type { AppProps } from 'next/app';
import { ExperimentationProvider } from '@getexperimently/react-sdk';
import Layout from '@/components/Layout';
import ControlvarPanel from '@/components/ControlvarPanel';
import { EventLogProvider } from '@/lib/eventLog';
import { API_KEY, API_URL } from '@/lib/env';
import { getOrCreateVisitor, type Visitor } from '@/lib/visitor';

export default function ShopLabApp({ Component, pageProps }: AppProps) {
  // The visitor id only exists in the browser, so wait for it before rendering any
  // page: every hook below needs a stable user id for sticky assignments.
  const [visitor, setVisitor] = useState<Visitor | null>(null);
  useEffect(() => {
    setVisitor(getOrCreateVisitor());
  }, []);

  const config = useMemo(() => ({ apiKey: API_KEY, baseUrl: API_URL, timeoutMs: 5000 }), []);
  const user = useMemo(() => (visitor ? { userId: visitor.id, attributes: visitor.attributes } : null), [visitor]);

  return (
    <>
      <Head>
        <title>ShopLab — outdoor gear, tested live</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="description" content="ShopLab is a demo storefront running live experiments on Controlvar." />
      </Head>
      {user ? (
        <ExperimentationProvider config={config} user={user}>
          <EventLogProvider>
            <Layout>
              <Component {...pageProps} />
            </Layout>
            <ControlvarPanel />
          </EventLogProvider>
        </ExperimentationProvider>
      ) : (
        <div className="min-h-screen bg-neutral-50" aria-busy="true" aria-label="Loading ShopLab" />
      )}
    </>
  );
}
