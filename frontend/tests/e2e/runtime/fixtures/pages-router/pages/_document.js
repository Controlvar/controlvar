import { Head, Html, Main, NextScript } from "next/document";

// The snippet is the first element of <head>, as the install guide asks.
// The origins are the page-runtime specs' (runtime/editor.ts): the specs
// route them, and nothing listens on them.
export default function Document() {
  return (
    <Html lang="en">
      <Head>
        {/* Synchronous on purpose: its first run comes before any later script on the page. */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script
          src="/controlvar-visual.js"
          data-api-url="http://localhost:4100"
          data-api-key="test-website-key"
          data-editor-origin="http://localhost:4300"
        />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
