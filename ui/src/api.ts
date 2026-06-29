import type { RedemptionRequest, RunStatusResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export async function startRedemption(
  req: RedemptionRequest,
): Promise<{ run_id: string }> {
  const res = await fetch(`${API_BASE}/redemptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw new Error(`Failed to start run (${res.status})`);
  }
  return res.json();
}

export async function getRunStatus(runId: string): Promise<RunStatusResponse> {
  const res = await fetch(`${API_BASE}/status/${runId}`);
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
    const status = await getRunStatus(runId);
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
