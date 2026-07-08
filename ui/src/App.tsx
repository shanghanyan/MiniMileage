import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import AirportInput from "./AirportInput";
import DisconnectedPage from "./DisconnectedPage";
import LiveScrapePage from "./LiveScrapePage";
import {
  isKnownAirport,
  resolveAirport,
  routeHasFare,
} from "./airports";
import {
  ApiConnectionError,
  checkHealth,
  formatCpp,
  formatDollars,
  parseMiles,
  pollUntilComplete,
  startRedemption,
} from "./api";
import type { PipelineStep, QuoteResult, RunStatusResponse } from "./types";

const STEPS: { key: PipelineStep; label: string; n: number }[] = [
  { key: "route", label: "Route", n: 1 },
  { key: "gathering", label: "Gathering", n: 2 },
  { key: "crosscheck", label: "Cross-check", n: 3 },
  { key: "redemptions", label: "Redemptions", n: 4 },
];

type DemoKey = "A" | "B";
type View = "optimizer" | "debug";

const DEMOS: Record<
  DemoKey,
  {
    label: string;
    origin: string;
    dest: string;
    cabin: "economy" | "premium_economy" | "business" | "first";
    miles: string;
  }
> = {
  A: {
    label: "Demo A — Honest floor",
    origin: "LAX",
    dest: "JFK",
    cabin: "economy",
    miles: "20,000",
  },
  B: {
    label: "Demo B — Hidden value",
    origin: "LAX",
    dest: "IST",
    cabin: "business",
    miles: "90,000",
  },
};

function stepClass(
  stepKey: PipelineStep,
  activeStep: PipelineStep | null,
  stepsDone: PipelineStep[],
): string {
  if (activeStep === stepKey) return "step active";
  const order = STEPS.map((s) => s.key);
  const activeIdx = activeStep ? order.indexOf(activeStep) : -1;
  const stepIdx = order.indexOf(stepKey);
  if (stepsDone.includes(stepKey) && stepIdx < activeIdx) return "step done";
  if (
    stepsDone.includes(stepKey) &&
    activeStep === "redemptions" &&
    stepKey !== "redemptions"
  ) {
    return "step done";
  }
  return "step";
}

function verdictHeadline(verdict?: string): string {
  switch (verdict) {
    case "portal_only":
      return "Portal is your floor";
    case "comparable":
      return "Comparable to portal";
    case "best":
    case "tentative_best":
      return "Best verified route";
    default:
      return "Result";
  }
}

function verdictClass(verdict?: string): string {
  if (verdict === "best" || verdict === "tentative_best") return "verdict best";
  return "verdict portal";
}

function buildSubline(result: QuoteResult): string {
  const parts: string[] = [];
  if (result.flags?.length) {
    parts.push(result.flags.join(" · "));
  }
  if (result.live_award_space?.length) {
    parts.push(`${result.live_award_space.length} live seat source(s)`);
  } else if (
    result.verdict === "best" ||
    result.verdict === "tentative_best"
  ) {
    parts.push("chart verified");
  }
  if (result.fare_cents) {
    parts.push(`cash fare ${formatDollars(result.fare_cents)}`);
  }
  return parts.join(" · ") || "Verified across sources";
}

function matchesDemo(
  key: DemoKey,
  origin: string,
  dest: string,
  cabin: string,
  miles: string,
): boolean {
  const demo = DEMOS[key];
  return (
    resolveAirport(origin)?.code === demo.origin &&
    resolveAirport(dest)?.code === demo.dest &&
    cabin === demo.cabin &&
    parseMiles(miles) === parseMiles(demo.miles)
  );
}

function isDemoEnabled(key: DemoKey): boolean {
  const demo = DEMOS[key];
  return routeHasFare(demo.origin, demo.dest, demo.cabin);
}

export default function App() {
  const [view, setView] = useState<View>("optimizer");
  const [origin, setOrigin] = useState("LAX");
  const [dest, setDest] = useState("IST");
  const [cabin, setCabin] = useState<
    "economy" | "premium_economy" | "business" | "first"
  >("business");
  const [miles, setMiles] = useState("90,000");
  // No demo is preselected on load — the user opts into one explicitly (or types
  // a route that matches, which the change handlers below then highlight).
  const [selectedDemo, setSelectedDemo] = useState<DemoKey | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeStep, setActiveStep] = useState<PipelineStep | null>("route");
  const [stepsDone, setStepsDone] = useState<PipelineStep[]>([]);
  const [result, setResult] = useState<QuoteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [apiConnected, setApiConnected] = useState<boolean | null>(null);
  const [checkingConnection, setCheckingConnection] = useState(false);

  const probeConnection = useCallback(async () => {
    setCheckingConnection(true);
    const ok = await checkHealth();
    setApiConnected(ok);
    setCheckingConnection(false);
    return ok;
  }, []);

  useEffect(() => {
    void probeConnection();
  }, [probeConnection]);

  useEffect(() => {
    const intervalMs = apiConnected ? 15000 : 4000;
    const id = window.setInterval(() => {
      void probeConnection();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [apiConnected, probeConnection]);

  useEffect(() => {
    if (!import.meta.hot) return;
    const onDisconnect = () => setApiConnected(false);
    import.meta.hot.on("vite:ws:disconnect", onDisconnect);
    return () => {
      import.meta.hot?.off("vite:ws:disconnect", onDisconnect);
    };
  }, []);

  const originCode = resolveAirport(origin)?.code ?? origin;
  const destCode = resolveAirport(dest)?.code ?? dest;
  const airportsValid = isKnownAirport(origin) && isKnownAirport(dest);
  const routeSupported = routeHasFare(origin, dest, cabin);

  const displayPath = useMemo(() => {
    if (!result) return null;
    if (
      result.best_transfer &&
      (result.verdict === "best" || result.verdict === "tentative_best")
    ) {
      return result.best_transfer.label;
    }
    return "Capital One portal";
  }, [result]);

  const displayCpp = useMemo(() => {
    if (!result) return null;
    if (
      result.best_transfer &&
      (result.verdict === "best" || result.verdict === "tentative_best")
    ) {
      return result.best_transfer.cpp;
    }
    return result.portal_cpp ?? 0;
  }, [result]);

  function clearDemoSelection() {
    setSelectedDemo(null);
  }

  function applyDemo(key: DemoKey) {
    if (!isDemoEnabled(key)) return;
    const demo = DEMOS[key];
    setOrigin(demo.origin);
    setDest(demo.dest);
    setCabin(demo.cabin);
    setMiles(demo.miles);
    setSelectedDemo(key);
    setResult(null);
    setError(null);
    setActiveStep("route");
    setStepsDone([]);
  }

  function handleOriginChange(code: string) {
    setOrigin(code);
    if (matchesDemo("A", code, dest, cabin, miles)) setSelectedDemo("A");
    else if (matchesDemo("B", code, dest, cabin, miles)) setSelectedDemo("B");
    else clearDemoSelection();
  }

  function handleDestChange(code: string) {
    setDest(code);
    if (matchesDemo("A", origin, code, cabin, miles)) setSelectedDemo("A");
    else if (matchesDemo("B", origin, code, cabin, miles)) setSelectedDemo("B");
    else clearDemoSelection();
  }

  function handleCabinChange(
    next: "economy" | "premium_economy" | "business" | "first",
  ) {
    setCabin(next);
    clearDemoSelection();
  }

  function handleMilesChange(raw: string) {
    setMiles(raw);
    if (matchesDemo("A", origin, dest, cabin, raw)) setSelectedDemo("A");
    else if (matchesDemo("B", origin, dest, cabin, raw)) setSelectedDemo("B");
    else clearDemoSelection();
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!airportsValid) {
      setError("Choose a valid airport from the suggestions.");
      return;
    }
    if (!routeSupported) {
      setError(
        `No verified fare data for ${originCode}→${destCode} ${cabin}. Try a demo route or LAX→JFK economy / LAX→IST business.`,
      );
      return;
    }

    setLoading(true);
    setResult(null);
    setError(null);
    setActiveStep("route");
    setStepsDone([]);

    try {
      const { run_id } = await startRedemption({
        origin: originCode,
        dest: destCode,
        cabin,
        currency: "capital_one",
        miles: parseMiles(miles),
        card: "venture_x",
      });

      const finalStatus: RunStatusResponse = await pollUntilComplete(
        run_id,
        (status) => {
          setActiveStep(status.step);
          setStepsDone(status.steps_done);
        },
      );

      if (finalStatus.status === "error") {
        setError(finalStatus.message ?? finalStatus.error ?? "Run failed");
        if (finalStatus.result) setResult(finalStatus.result);
        return;
      }

      if (finalStatus.result) {
        setResult(finalStatus.result);
      }
    } catch (err) {
      if (err instanceof ApiConnectionError) {
        setApiConnected(false);
        setError(null);
        return;
      }
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
      setActiveStep("redemptions");
    }
  }

  if (apiConnected === false) {
    return (
      <DisconnectedPage
        onRetry={() => {
          void probeConnection();
        }}
        checking={checkingConnection}
      />
    );
  }

  return (
    <>
      <header>
        <div className="brand">
          <span className="mark">✦</span> Mileage
        </div>
        <div className="header-right">
          <nav className="page-nav" aria-label="Pages">
            <button
              type="button"
              className={`page-tab${view === "optimizer" ? " active" : ""}`}
              onClick={() => setView("optimizer")}
            >
              Optimizer
            </button>
            <button
              type="button"
              className={`page-tab${view === "debug" ? " active" : ""}`}
              onClick={() => setView("debug")}
            >
              Debug UI
            </button>
          </nav>
          {view === "optimizer" && (
            <nav className="steps" aria-label="Progress">
              {STEPS.map((s) => (
                <span
                  key={s.key}
                  className={stepClass(s.key, activeStep, stepsDone)}
                >
                  <span className="n">{s.n}</span>
                  {s.label}
                </span>
              ))}
            </nav>
          )}
        </div>
      </header>

      {view === "debug" && <LiveScrapePage />}

      {view === "optimizer" && (
      <main>
        <h1 className="wordmark">Mileage</h1>
        <p className="tagline">
          Turn your points into the right seat — verified across sources, never
          guessed.
        </p>

        <form
          className="card"
          aria-label="Plan your redemption"
          onSubmit={handleSubmit}
        >
          <p className="card-eyebrow">Plan a redemption</p>
          <div className="row">
            <AirportInput
              id="from"
              label="From"
              placeholder="LAX"
              value={origin}
              excludeCode={destCode}
              onChange={handleOriginChange}
            />
            <AirportInput
              id="to"
              label="To"
              placeholder="IST"
              value={dest}
              excludeCode={originCode}
              onChange={handleDestChange}
            />
          </div>
          <div className="row">
            <div className="field select">
              <label htmlFor="cabin">Cabin</label>
              <select
                id="cabin"
                value={cabin}
                onChange={(e) =>
                  handleCabinChange(
                    e.target.value as
                      | "economy"
                      | "premium_economy"
                      | "business"
                      | "first",
                  )
                }
              >
                <option value="economy">Economy</option>
                <option value="premium_economy">Premium economy</option>
                <option value="business">Business</option>
                <option value="first">First</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="points">Capital One miles</label>
              <input
                id="points"
                type="text"
                placeholder="85,000"
                value={miles}
                onChange={(e) => handleMilesChange(e.target.value)}
                required
              />
            </div>
          </div>

          <div className="demo-row">
            {(Object.keys(DEMOS) as DemoKey[]).map((key) => {
              const selected = selectedDemo === key;
              const enabled = isDemoEnabled(key);
              return (
                <button
                  key={key}
                  type="button"
                  className={`demo-btn${selected ? " selected" : ""}`}
                  disabled={loading || !enabled}
                  onClick={() => applyDemo(key)}
                >
                  {DEMOS[key].label}
                </button>
              );
            })}
          </div>

          <button
            className="cta"
            type="submit"
            disabled={loading || !airportsValid || !routeSupported}
          >
            {loading ? "Running pipeline…" : "Find best routes"}
          </button>
          <p className="note">
            The aggregator and curated charts run in parallel — we only name a
            winner when the numbers are <b>verified</b>.
          </p>
        </form>

        {error && (
          <div className="error-box" role="alert">
            {error}
          </div>
        )}

        {result && result.verdict && displayPath && displayCpp !== null && (
          <>
            <section
              className={verdictClass(result.verdict)}
              aria-label="Verdict"
            >
              <div>
                <div className="label">{verdictHeadline(result.verdict)}</div>
                <div className="path">{displayPath}</div>
                <div className="sub">{buildSubline(result)}</div>
                {result.rationale && (
                  <div className="sub">{result.rationale}</div>
                )}
              </div>
              <div className="cpp">
                {formatCpp(displayCpp)}
                <small>per C1 mile</small>
              </div>
            </section>

            {result.options && result.options.length > 0 && (
              <section className="options" aria-label="Ranked redemptions">
                <h2>Ranked redemptions</h2>
                {result.options.map((opt) => {
                  const highlight =
                    result.best_transfer &&
                    opt.label === result.best_transfer.label &&
                    (result.verdict === "best" ||
                      result.verdict === "tentative_best");
                  return (
                    <div
                      key={opt.label}
                      className={`option-row${highlight ? " highlight" : ""}`}
                    >
                      <div>
                        <div>{opt.label}</div>
                        <div className="meta">
                          {opt.source_points.toLocaleString()} pts · conf{" "}
                          {opt.confidence.toFixed(2)}
                          {!opt.affordable ? " · need more points" : ""}
                          {opt.flags.length ? ` · ${opt.flags.join(", ")}` : ""}
                        </div>
                      </div>
                      <div>{formatCpp(opt.cpp)}</div>
                    </div>
                  );
                })}
              </section>
            )}
          </>
        )}
      </main>
      )}
    </>
  );
}
