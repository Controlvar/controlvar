// The customer page every framework fixture of the page-runtime project
// renders (#1545): 25 text targets (#t-1..#t-25), 25 images inside a
// <picture> (#i-1..#i-25), and #hydrated, which says "hydrated" once React
// has hydrated the page. Written without JSX, and taking React as an
// argument, so that Next and the esbuild builds (React 18.3.1 and 19.3)
// render the same markup.
export const TARGETS = 25;

/** The targets numbered *from* to *to*, as React elements made with *h*. */
export function targets(h, from = 1, to = TARGETS) {
  const out = [];
  for (let n = from; n <= to; n += 1) {
    out.push(h("p", { id: `t-${n}`, key: `t-${n}` }, `Original server text ${n}`));
    out.push(
      h(
        "picture",
        { key: `p-${n}` },
        h("source", { srcSet: "/a.webp", type: "image/webp" }),
        h("img", { id: `i-${n}`, src: "/a.png", alt: "", width: 1, height: 1 }),
      ),
    );
  }
  return out;
}

/** The whole page as one component; *split* puts the targets in two sibling Suspense boundaries. */
export function makeApp(React, { split = false } = {}) {
  const h = React.createElement;
  function Hydrated() {
    const [hydrated, setHydrated] = React.useState(false);
    React.useEffect(() => setHydrated(true), []);
    return h("p", { id: "hydrated" }, hydrated ? "hydrated" : "server");
  }
  return function App() {
    const body = split
      ? [
          h(React.Suspense, { key: "a", fallback: null }, h("div", { id: "a" }, targets(h, 1, 12))),
          h(React.Suspense, { key: "b", fallback: null }, h("div", { id: "b" }, targets(h, 13, TARGETS))),
        ]
      : targets(h);
    return h("main", null, body, h(Hydrated, null));
  };
}
