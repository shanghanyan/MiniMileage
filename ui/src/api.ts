import type { RedemptionRequest, RunStatusResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Phase 4 (multi-user): when the API runs with MILEAGE_AUTH=1, the bearer token
// IS the user id. Pick it up from ?token=alice or VITE_API_TOKEN so the same UI
// can act as different users (open two tabs to see per-user verdicts). When auth
// is off (the default), this is simply absent and the server uses "local".
function authToken(): string | null {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("token");
    if (fromUrl) return fromUrl;
  } catch {
    /* non-browser / SSR */
  }
  return (import.meta.env.VITE_API_TOKEN as string | undefined) ?? null;
}

function authHeaders(): Record<string, string> {
  const token = authToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/health`, {
      method: "GET",
      cache: "no-store",
    });
    return res.ok;
  } catch {
    return false;
  }
}

export class ApiConnectionError extends Error {
  constructor(message = "Cannot reach the Mileage API.") {
    super(message);
    this.name = "ApiConnectionError";
  }
}

async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (err) {
    if (
      err instanceof TypeError ||
      (err instanceof Error &&
        /failed to fetch|network|load failed/i.test(err.message))
    ) {
      throw new ApiConnectionError();
    }
    throw err;
  }
}

export async function startRedemption(
  req: RedemptionRequest,
): Promise<{ run_id: string }> {
  const res = await apiFetch(`${API_BASE}/redemptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw new Error(`Failed to start run (${res.status})`);
  }
  return res.json();
}

export async function getRunStatus(runId: string): Promise<RunStatusResponse> {
  const res = await apiFetch(`${API_BASE}/status/${runId}`, {
    headers: { ...authHeaders() },
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch status (${res.status})`);
  }
  return res.json();
}

export async function pollUntilComplete(
  runId: string,
  onProgress: (status: RunStatusResponse) => void,
  intervalMs = 400,
): Promise<RunStatusResponse> {
  for (;;) {
    let status: RunStatusResponse;
    try {
      status = await getRunStatus(runId);
    } catch (err) {
      if (err instanceof ApiConnectionError) throw err;
      throw err;
    }
    onProgress(status);
    if (status.status === "complete" || status.status === "error") {
      return status;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export function parseMiles(raw: string): number {
  return Number(raw.replace(/[^0-9]/g, "")) || 0;
}

export function formatCpp(cpp: number): string {
  return `${cpp.toFixed(1)}¢`;
}

export function formatDollars(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}
