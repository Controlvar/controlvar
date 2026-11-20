import Link from "next/link";

// A heading and links that stay on every page, so a client-side navigation
// changes only what is under them.
export default function App({ Component, pageProps }) {
  return (
    <>
      <h1 id="t">Original</h1>
      <nav>
        <Link id="to-home" href="/">
          home
        </Link>{" "}
        <Link id="to-other" href="/other">
          other
        </Link>
      </nav>
      <Component {...pageProps} />
    </>
  );
}
