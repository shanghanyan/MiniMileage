import { useState } from "react";
import { ApiConnectionError, runLiveScrape } from "./api";
import type {
  LiveScrapeResponse,
  ScrapeStatus,
  ScrapeTarget,
} from "./types";

function statusLabel(status: ScrapeStatus): string {
  if (status === "ok") return "OK";
  if (status === "warn") return "WARN";
  return "FAIL";
}

function TargetCard({ target }: { target: ScrapeTarget }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`scrape-target ${target.status}`}>
      <div className="scrape-target-head">
        <span className={`pill ${target.status}`}>
          {statusLabel(target.status)}
        </span>
        <div className="scrape-target-title">
          <span className="scrape-name">{target.name}</span>
          <span className={`role-tag ${target.role}`}>{target.role}</span>
          {target.program && (
            <span className="scrape-meta-chip">{target.program}</span>
          )}
          <span className="scrape-meta-chip">{target.format}</span>
          {target.reclassified && (
            <span className="scrape-meta-chip muted">
              downgraded from fail
            </span>
          )}
        </div>
        <span className="scrape-rows">
          {target.rows} row{target.rows === 1 ? "" : "s"}
        </span>
      </div>

      <a
        className="scrape-url"
        href={target.url}
        target="_blank"
        rel="noreferrer"
      >
        {target.url}
      </a>

      <p className="scrape-detail">{target.detail}</p>

      {target.sample.length > 0 && (
        <button
          type="button"
          className="scrape-sample-toggle"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Hide" : "Show"} scraped sample ({target.sample.length})
        </button>
      )}
      {open && target.sample.length > 0 && (
        <pre className="scrape-sample">
          {JSON.stringify(target.sample, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default function LiveScrapePage() {
  const [loading, setLoading] = useState(false);
  const [offline, setOffline] = useState(false);
  const [report, setReport] = useState<LiveScrapeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleRun() {
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const res = await runLiveScrape(offline);
      setReport(res);
    } catch (err) {
      if (err instanceof ApiConnectionError) {
        setError(
          "Cannot reach the Mileage API. Start it with `uvicorn mileage.api.app:app`.",
        );
      } else {
        setError(err instanceof Error ? err.message : "Live scrape failed");
      }
    } finally {
      setLoading(false);
    }
  }

  const summary = report?.summary;

  return (
    <main className="scrape-main">
      <h1 className="scrape-h1">Live scrape</h1>
      <p className="scrape-lede">
        Walk every source in <code>sources.yaml</code> through the real
        fetch&nbsp;→&nbsp;parse&nbsp;→&nbsp;resolve stack. See exactly what was
        pulled from each site — and, when a source comes back empty, the specific
        reason it failed.
      </p>

      <div className="scrape-controls">
        <button
          className="cta scrape-run"
          type="button"
          onClick={handleRun}
          disabled={loading}
        >
          {loading ? "Scraping…" : "Run live scrape"}
        </button>
        <label className="scrape-offline">
          <input
            type="checkbox"
            checked={offline}
            onChange={(e) => setOffline(e.target.checked)}
            disabled={loading}
          />
          Offline (fixtures only — fast, no network)
        </label>
      </div>

      {loading && (
        <p className="scrape-note">
          Running the full fetch chain (httpx → TLS impersonation → Wayback) for
          every target. A live run can take tens of seconds.
        </p>
      )}

      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      {summary && report && (
        <>
          <section
            className={`scrape-verdict ${
              summary.all_primaries_ok ? "good" : "bad"
            }`}
            aria-label="Scrape verdict"
          >
            <div>
              <div className="label">
                {summary.all_primaries_ok
                  ? "Every program has a working primary"
                  : "A program is missing its primary source"}
              </div>
              <div className="scrape-verdict-sub">
                {summary.primary_ok}/
                {summary.primary_ok +
                  summary.primary_warn +
                  summary.primary_fail}{" "}
                primaries resolved · {summary.fallback_warn} fallback warning
                {summary.fallback_warn === 1 ? "" : "s"} · {summary.programs}{" "}
                programs
                {report.offline ? " · offline (fixtures only)" : ""}
              </div>
            </div>
            <div className="scrape-verdict-mark">
              {summary.all_primaries_ok ? "✓" : "✕"}
            </div>
          </section>

          <section className="scrape-programs" aria-label="Program coverage">
            <h2>Program coverage</h2>
            {report.programs.map((p) => (
              <div
                key={p.program}
                className={`program-row ${
                  p.has_working_primary ? "good" : "bad"
                }`}
              >
                <div className="program-name">
                  <span
                    className={`pill ${p.has_working_primary ? "ok" : "fail"}`}
                  >
                    {p.has_working_primary ? "OK" : "NO PRIMARY"}
                  </span>
                  {p.program}
                </div>
                <div className="program-sources">
                  {p.primaries.map((s) => (
                    <span key={s.name} className={`source-chip ${s.status}`}>
                      {s.name}
                    </span>
                  ))}
                  {p.fallbacks.map((s) => (
                    <span
                      key={s.name}
                      className={`source-chip fallback ${s.status}`}
                    >
                      {s.name}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </section>

          <section className="scrape-targets" aria-label="Per-source results">
            <h2>Every source ({report.targets.length})</h2>
            {report.targets.map((t) => (
              <TargetCard key={t.name} target={t} />
            ))}
          </section>
        </>
      )}
    </main>
  );
}
