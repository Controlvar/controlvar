// The browser half of a standalone React fixture (#1545): hydrates the
// server-rendered page with React's default error handling. A page whose
// window.__holdHydration is set waits for window.__hydrate() instead, so a
// spec can hydrate it at a time it chooses. SPLIT is set by the build.
import * as React from "react";
import { hydrateRoot } from "react-dom/client";
import { makeApp } from "../shared/app.mjs";

/* global SPLIT */
const App = makeApp(React, { split: SPLIT });
const hydrate = () => hydrateRoot(document.getElementById("root"), React.createElement(App));
if (window.__holdHydration) window.__hydrate = hydrate;
else hydrate();
