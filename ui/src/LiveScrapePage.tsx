import { useEffect, useState } from "react";
import { ApiConnectionError, getDailyScrape, getScrapeInventory, runLiveScrape } from "./api";
import type {
  DailyScrapeResponse,
  DiscoveryChannel,
  LiveScrapeResponse,
  ProviderPath,
  ScrapeInventoryResponse,
  ScrapeStatus,
  ScrapeTarget,
} from "./types";

function statusLabel(status: ScrapeStatus): string {
  if (status === "ok") return "OK";
  if (status === "warn") return "WARN";
  return "FAIL";
}

function healthLabel(health: string): string {
  if (health === "healthy") return "OK";
  if (health === "degraded") return "WARN";
  return "DOWN";
}

function healthClass(health: string): string {
  if (health === "healthy") return "ok";
  if (health === "degraded") return "warn";
  return "fail";
}

function kindLabel(kind: DiscoveryChannel["kind"]): string {
  if (kind === "email") return "email";
  if (kind === "blog") return "blog RSS";
  return "YouTube";
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
          {target.block_type && target.block_type !== "none" && (
            <span className="scrape-meta-chip muted">
              block: {target.block_type}
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

function DiscoveryCard({ channel }: { channel: DiscoveryChannel }) {
  const status = channel.ready ? "ok" : "fail";
  return (
    <div className={`scrape-target inventory ${status}`}>
      <div className="scrape-target-head">
        <span className={`pill ${status}`}>
          {channel.ready ? "READY" : "NOT READY"}
        </span>
        <div className="scrape-target-title">
          <span className="scrape-name">{channel.name}</span>
          <span className="scrape-meta-chip">{kindLabel(channel.kind)}</span>
          <span className="scrape-meta-chip muted">
            trust {channel.trust.toFixed(2)}
          </span>
        </div>
      </div>
      {channel.url && (
        <a
          className="scrape-url"
          href={channel.url}
          target="_blank"
          rel="noreferrer"
        >
          {channel.url}
        </a>
      )}
      <p className="scrape-detail">{channel.detail}</p>
      <p className="scrape-detail muted">
        Run: <code>{channel.command}</code>
      </p>
    </div>
  );
}

function ProviderCard({ provider }: { provider: ProviderPath }) {
  const status = provider.disabled
    ? "warn"
    : healthClass(provider.health);
  return (
    <div className={`scrape-target inventory ${status}`}>
      <div className="scrape-target-head">
        <span className={`pill ${status}`}>
          {provider.disabled ? "DISABLED" : healthLabel(provider.health)}
        </span>
        <div className="scrape-target-title">
          <span className="scrape-name">{provider.name}</span>
          {provider.layers.map((layer) => (
            <span key={layer} className="scrape-meta-chip">
              {layer}
            </span>
          ))}
          <span className="scrape-meta-chip muted">
            trust {provider.trust.toFixed(2)}
          </span>
        </div>
      </div>
      {provider.note && <p className="scrape-detail">{provider.note}</p>}
      {provider.config_hint && provider.health !== "healthy" && (
        <p className="scrape-detail muted">
          Needs: <code>{provider.config_hint}</code>
        </p>
      )}
      {provider.monthly_limit != null && (
        <p className="scrape-detail muted">
          Monthly quota: {provider.monthly_limit}
        </p>
      )}
    </div>
  );
}

function InventorySection({ inventory }: { inventory: ScrapeInventoryResponse }) {
  const { summary, discovered } = inventory;
  const blogs = inventory.discovery.filter((d) => d.kind === "blog");
  const youtube = inventory.discovery.filter((d) => d.kind === "youtube");
  const email = inventory.discovery.filter((d) => d.kind === "email");

  return (
    <>
      <section className="scrape-inventory" aria-label="System inventory">
        <h2>All scrape paths</h2>
        <p className="scrape-section-lede">
          {summary.chart_targets} chart targets in{" "}
          <code>sources.yaml</code> (checked below) ·{" "}
          {summary.discovery_ready}/{summary.discovery_channels} discovery
          channels ready · {summary.providers_healthy}/{summary.providers}{" "}
          providers healthy
        </p>

        <h3 className="scrape-subhead">Discovery intake</h3>
        <p className="scrape-section-lede">
          Email, blog RSS, and YouTube transcripts — swept on Live Scrape and
          daily cron. Newsletter emails also follow embedded blog/YouTube links.
          Rows persist to <code>discovered_charts.json</code>.
        </p>

        {discovered.row_count > 0 && (
          <div className="scrape-discovered-meta">
            Last persisted discovery: {discovered.row_count} row
            {discovered.row_count === 1 ? "" : "s"}
            {discovered.updated_at
              ? ` · ${new Date(discovered.updated_at).toLocaleString()}`
              : ""}
            {Object.keys(discovered.by_intake).length > 0 && (
              <>
                {" "}
                ·{" "}
                {Object.entries(discovered.by_intake)
                  .map(([k, n]) => `${k}: ${n}`)
                  .join(" · ")}
              </>
            )}
            {discovered.stale_programs.length > 0 && (
              <>
                {" "}
                · stale: {discovered.stale_programs.join(", ")}
              </>
            )}
          </div>
        )}

        <h4 className="scrape-kind-head">Email ({email.length})</h4>
        {email.map((ch) => (
          <DiscoveryCard key={ch.name} channel={ch} />
        ))}

        <h4 className="scrape-kind-head">Blog RSS ({blogs.length})</h4>
        {blogs.map((ch) => (
          <DiscoveryCard key={ch.name} channel={ch} />
        ))}

        <h4 className="scrape-kind-head">YouTube transcripts ({youtube.length})</h4>
        {youtube.map((ch) => (
          <DiscoveryCard key={ch.name} channel={ch} />
        ))}

        <h3 className="scrape-subhead">Federated providers</h3>
        <p className="scrape-section-lede">
          Cash fares, award space, and curated fallbacks — outside the{" "}
          <code>sources.yaml</code> chart scrape walk.
        </p>
        {inventory.providers.map((p) => (
          <ProviderCard key={p.name} provider={p} />
        ))}
      </section>
    </>
  );
}

export default function LiveScrapePage() {
  const [loading, setLoading] = useState(false);
  const [daily, setDaily] = useState<DailyScrapeResponse | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  const [report, setReport] = useState<LiveScrapeResponse | null>(null);
  const [inventory, setInventory] = useState<ScrapeInventoryResponse | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [inventoryError, setInventoryError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setInventoryLoading(true);
      setInventoryError(null);
      try {
        const [inv, dailyRes] = await Promise.all([
          getScrapeInventory(),
          getDailyScrape().catch(() => ({ found: false })),
        ]);
        if (!cancelled) {
          setInventory(inv);
          setDaily(dailyRes.found ? dailyRes : null);
        }
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiConnectionError) {
            setInventoryError(
              "Cannot reach the Mileage API. Start it with `uvicorn mileage.api.app:app`.",
            );
          } else {
            setInventoryError(
              err instanceof Error ? err.message : "Inventory load failed",
            );
          }
        }
      } finally {
        if (!cancelled) setInventoryLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRun() {
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const res = await runLiveScrape(offline);
      setReport(res);
      try {
        const inv = await getScrapeInventory();
        setInventory(inv);
      } catch {
        // inventory refresh is best-effort
      }
      try {
        const dailyRes = await getDailyScrape();
        if (dailyRes.found) setDaily(dailyRes);
      } catch {
        // daily refresh is best-effort
      }
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
      <h1 className="scrape-h1">Debug UI</h1>
      <p className="scrape-lede">
        Scraper diagnostics and manual runs. Production data is refreshed by{" "}
        <code>mileage scrape-daily</code> (cron) and stored in Redis Cloud when{" "}
        <code>MILEAGE_REDIS_URL</code> is set. Use the button below for an
        on-demand live scrape without waiting for the schedule.
      </p>

      {daily && (
        <section className="scrape-discovered-meta" aria-label="Last daily scrape">
          <h2 className="scrape-subhead">Last daily scrape</h2>
          <p>
            {daily.completed_at
              ? new Date(daily.completed_at).toLocaleString()
              : "unknown time"}
            {daily.storage && ` · stored via ${daily.storage}`}
            {daily.storage_backend && ` (${daily.storage_backend})`}
            {daily.discovery && (
              <>
                {" "}
                · discovery: {daily.discovery.row_count} row
                {daily.discovery.row_count === 1 ? "" : "s"}
              </>
            )}
            {daily.scrape?.summary && (
              <>
                {" "}
                · primaries{" "}
                {daily.scrape.summary.all_primaries_ok ? "OK" : "FAIL"}
              </>
            )}
          </p>
          <p className="scrape-detail muted">
            Cron example: <code>0 6 * * * ./scripts/daily_scrape.sh</code>
          </p>
        </section>
      )}

      {!daily && !inventoryLoading && (
        <p className="scrape-note">
          No daily scrape snapshot yet — run{" "}
          <code>mileage scrape-daily</code> or wait for cron.
        </p>
      )}

      {inventoryLoading && (
        <p className="scrape-note">Loading system inventory…</p>
      )}
      {inventoryError && (
        <div className="error-box" role="alert">
          {inventoryError}
        </div>
      )}
      {inventory && <InventorySection inventory={inventory} />}

      <section className="scrape-chart-run" aria-label="Chart source live scrape">
        <h2>Chart sources ({inventory?.summary.chart_targets ?? "…"})</h2>
        <p className="scrape-section-lede">
          Walk every entry in <code>sources.yaml</code> through fetch&nbsp;→
          parse&nbsp;→&nbsp;resolve, then sweep discovery intake (email + blogs
          + YouTube).
        </p>

        <div className="scrape-controls">
          <button
            className="cta scrape-run"
            type="button"
            onClick={handleRun}
            disabled={loading}
          >
            {loading ? "Scraping…" : "Run live scrape + discovery"}
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
            Running chart scrape and discovery intake (IMAP + RSS + YouTube).
            A live run can take tens of seconds to a few minutes.
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

            {report.discovery && (
              <section
                className="scrape-discovered-meta"
                aria-label="Discovery intake result"
              >
                <h3 className="scrape-subhead">Discovery intake</h3>
                <p>
                  {report.discovery.detail}
                  {report.discovery.email_docs > 0 &&
                    ` · ${report.discovery.email_docs} email(s)`}
                  {report.discovery.blog_new > 0 &&
                    ` · ${report.discovery.blog_new} new blog post(s)`}
                  {report.discovery.transcript_new > 0 &&
                    ` · ${report.discovery.transcript_new} new video(s)`}
                  {Object.keys(report.discovery.by_intake).length > 0 && (
                    <>
                      {" "}
                      ·{" "}
                      {Object.entries(report.discovery.by_intake)
                        .map(([k, n]) => `${k}: ${n}`)
                        .join(" · ")}
                    </>
                  )}
                  {report.discovery.email_links_followed > 0 && (
                    <>
                      {" "}
                      · {report.discovery.email_links_followed} email link(s)
                      followed
                    </>
                  )}
                  {report.discovery.stale_programs.length > 0 && (
                    <> · stale: {report.discovery.stale_programs.join(", ")}</>
                  )}
                </p>
              </section>
            )}

            <section className="scrape-programs" aria-label="Program coverage">
              <h3 className="scrape-subhead">Program coverage</h3>
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
              <h3 className="scrape-subhead">
                Every chart source ({report.targets.length})
              </h3>
              {report.targets.map((t) => (
                <TargetCard key={t.name} target={t} />
              ))}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
