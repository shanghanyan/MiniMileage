export type PipelineStep = "route" | "gathering" | "crosscheck" | "redemptions";
export type RunStatus = "pending" | "running" | "complete" | "error";

export interface RedemptionRequest {
  origin: string;
  dest: string;
  cabin: "economy" | "premium_economy" | "business" | "first";
  currency: string;
  miles: number;
  card: "venture" | "venture_x";
}

export interface PathOption {
  label: string;
  kind: "portal" | "transfer";
  cpp: number;
  source_points: number;
  affordable: boolean;
  confidence: number;
  flags: string[];
}

export interface QuoteResult {
  route: string;
  verdict?: string;
  rationale?: string;
  flags?: string[];
  fare_cents?: number;
  fare_flags?: string[];
  portal_cpp?: number;
  best_transfer?: {
    label: string;
    cpp: number;
    source_points: number;
    flags: string[];
  };
  options?: PathOption[];
  live_award_space?: Array<{
    program: string;
    miles: number;
    seats_available: number;
    flags: string[];
  }>;
  error?: string;
  message?: string;
}

export interface RunStatusResponse {
  run_id: string;
  status: RunStatus;
  step: PipelineStep;
  steps_done: PipelineStep[];
  result?: QuoteResult;
  error?: string;
  message?: string;
}

export type ScrapeStatus = "ok" | "warn" | "fail";

export interface ScrapeTarget {
  name: string;
  url: string;
  role: "primary" | "fallback";
  format: string;
  provides: "chart" | "award";
  trust: number;
  status: ScrapeStatus;
  detail: string;
  rows: number;
  program?: string | null;
  resolved?: string | null;
  reclassified: boolean;
  sample: Array<Record<string, unknown>>;
}

export interface ScrapeProgramEntry {
  name: string;
  status: ScrapeStatus;
  detail: string;
}

export interface ScrapeProgram {
  program: string;
  has_working_primary: boolean;
  primaries: ScrapeProgramEntry[];
  fallbacks: ScrapeProgramEntry[];
}

export interface ScrapeSummary {
  total: number;
  programs: number;
  all_primaries_ok: boolean;
  primary_ok: number;
  primary_warn: number;
  primary_fail: number;
  fallback_ok: number;
  fallback_warn: number;
}

export interface LiveScrapeDiscoveryResult {
  row_count: number;
  email_docs: number;
  blog_new: number;
  transcript_new: number;
  email_links_followed: number;
  by_intake: Record<string, number>;
  stale_programs: string[];
  used_fixtures: boolean;
  detail: string;
}

export interface DailyScrapeResponse {
  found: boolean;
  storage?: string | null;
  storage_backend?: string | null;
  completed_at?: string | null;
  stored_at?: string | null;
  discovery?: LiveScrapeDiscoveryResult | null;
  scrape?: {
    summary?: ScrapeSummary;
  } | null;
}

export interface LiveScrapeResponse {
  offline: boolean;
  targets: ScrapeTarget[];
  programs: ScrapeProgram[];
  summary: ScrapeSummary;
  discovery?: LiveScrapeDiscoveryResult | null;
}

export interface DiscoveryChannel {
  kind: "email" | "blog" | "youtube";
  name: string;
  url?: string | null;
  trust: number;
  ready: boolean;
  command: string;
  detail: string;
}

export interface ProviderPath {
  name: string;
  health: string;
  trust: number;
  layers: string[];
  disabled: boolean;
  monthly_limit?: number | null;
  config_hint?: string | null;
  note?: string | null;
}

export interface DiscoveredChartsMeta {
  updated_at?: string | null;
  row_count: number;
  by_intake: Record<string, number>;
  stale_programs: string[];
}

export interface ScrapeInventorySummary {
  chart_targets: number;
  discovery_channels: number;
  discovery_ready: number;
  providers: number;
  providers_healthy: number;
  discovered_rows: number;
}

export interface ScrapeInventoryResponse {
  discovery: DiscoveryChannel[];
  providers: ProviderPath[];
  discovered: DiscoveredChartsMeta;
  summary: ScrapeInventorySummary;
}
