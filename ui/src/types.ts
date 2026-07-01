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

export interface LiveScrapeResponse {
  offline: boolean;
  targets: ScrapeTarget[];
  programs: ScrapeProgram[];
  summary: ScrapeSummary;
}
