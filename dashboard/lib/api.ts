// Server-side access to the TrailGuard API. API keys never reach the browser.
import { cookies } from "next/headers";
import { readFileSync } from "node:fs";
import path from "node:path";

function setting(name: string): string | undefined {
  if (process.env[name]) return process.env[name];
  // In development the dashboard shares the project's .env one folder up.
  try {
    const file = readFileSync(path.join(process.cwd(), "..", ".env"), "utf8");
    const line = file.split("\n").find((l) => l.startsWith(`${name}=`));
    return line?.slice(name.length + 1).trim();
  } catch {
    return undefined;
  }
}

export function desks(): Record<string, string> {
  const pairs = (setting("TRAILGUARD_API_KEYS") ?? "").split(",").filter(Boolean);
  return Object.fromEntries(pairs.map((p) => p.split(":", 2) as [string, string]));
}

export async function currentDesk(): Promise<string> {
  const all = Object.keys(desks());
  const chosen = (await cookies()).get("desk")?.value;
  return chosen && all.includes(chosen) ? chosen : all[0];
}

export class ApiError extends Error {}

export async function api<T>(pathname: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const desk = await currentDesk();
  const base = setting("TRAILGUARD_API_URL") ?? "http://127.0.0.1:8000";
  let response: Response;
  try {
    response = await fetch(base + pathname, {
      method: init?.method ?? "GET",
      headers: { "X-API-Key": desks()[desk] ?? "", "Content-Type": "application/json" },
      body: init?.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(`The TrailGuard API at ${base} is not reachable. Start it, then reload.`);
  }
  if (!response.ok) {
    const detail = await response.json().then((j) => j.detail).catch(() => null);
    throw new ApiError(typeof detail === "string" ? detail : `The API answered ${response.status}.`);
  }
  return response.json();
}

export type CaseRow = {
  case_id: string; reporting_bank: string; reported_at: string; victim_account: string;
  agent_outcome: string | null; minutes_since_report: number; stolen_kobo: number;
  exited_kobo: number; in_accounts_kobo: number; held_kobo: number; holds_awaiting_review: number;
};
export type Hop = {
  seq: number; hop: number; transaction_id: string; ts: string; sender_bank: string; sender_account: string;
  receiver_bank: string | null; receiver_account: string | null; amount_kobo: number; tainted_kobo: number;
  channel: string; endpoint: string | null;
};
export type Hold = {
  hold_id: number; account: string; bank_id: string; case_id: string | null; requested_kobo: number;
  held_kobo: number | null; reason: string; source: string; status: string; placed_at: string;
  expires_at: string; reminders: number; recommended_kobo: number | null; recommendation: string | null; review: string | null; reviewed_by: string | null; mine?: boolean;
};
export type TrailAccount = {
  account: string; bank_id: string; kyc_tier: number; opened_at: string; stolen_in_account_kobo: number;
  alert: { score: number; first_flagged_at: string; reasons: string[] } | null;
};
export type Run = {
  run_id: number; as_of: string; model: string; outcome: string | null; summary: string | null;
  steps: { tool: string; input: Record<string, unknown>; failed: boolean }[] | null;
};
export type CaseDetail = CaseRow & {
  now: string; hops: Hop[]; holdings: { account: string; tainted_kobo: number }[];
  accounts: TrailAccount[]; holds: Hold[]; runs: Run[];
  evidence: { pack_id: number; built_at: string; hash: string } | null;
  str_draft: { draft_id: number; drafted_at: string; status: string; body: { narrative: string } } | null;
};
export type Me = { bank: string; now: string; simulated_clock: boolean };
