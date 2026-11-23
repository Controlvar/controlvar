import { createElement } from "react";
import { targets } from "../../shared/app.mjs";
import Hydrated from "./Hydrated";

export default function Page() {
  return (
    <main>
      {targets(createElement)}
      <Hydrated />
    </main>
  );
}
