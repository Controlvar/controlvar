"use client";
import { useEffect, useState } from "react";

// Shows "hydrated" once React has hydrated the page on the client.
export default function Hydrated() {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);
  return <p id="hydrated">{hydrated ? "hydrated" : "server"}</p>;
}
