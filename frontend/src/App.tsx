import { useEffect, useState } from "react";
import { api } from "./api";
import HomePage from "./components/HomePage";
import HowItWorks from "./components/HowItWorks";
import IncidentDetail from "./components/IncidentDetail";
import type { PublicConfig } from "./types";

type Route = { page: "home" } | { page: "incident"; id: number } | { page: "how" };

// Minimal hash routing: "#/", "#/incidents/7", "#/how-it-works".
function parseRoute(): Route {
  const hash = window.location.hash;
  const match = hash.match(/^#\/incidents\/(\d+)/);
  if (match) return { page: "incident", id: Number(match[1]) };
  if (hash.startsWith("#/how-it-works")) return { page: "how" };
  return { page: "home" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseRoute);
  const [config, setConfig] = useState<PublicConfig | null>(null);

  useEffect(() => {
    const onChange = () => {
      setRoute(parseRoute());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  return (
    <>
      <header className="topbar">
        <a href="#/" className="brand">
          TraceOps
        </a>
        <nav className="nav">
          <a href="#/" className={route.page !== "how" ? "active" : ""}>
            Dashboard
          </a>
          <a href="#/how-it-works" className={route.page === "how" ? "active" : ""}>
            How it works
          </a>
          {config && (
            <a href={config.grafana_url} target="_blank" rel="noreferrer" className="muted">
              Raw data (Grafana) ↗
            </a>
          )}
        </nav>
      </header>
      <main className="layout">
        {route.page === "home" && <HomePage />}
        {route.page === "incident" && <IncidentDetail id={route.id} config={config} />}
        {route.page === "how" && <HowItWorks />}
      </main>
    </>
  );
}
