import type { CaseRow, Hold } from "./api";

export const naira = (kobo: number | null | undefined) =>
  kobo == null ? "–" : "₦" + Math.round(kobo / 100).toLocaleString("en-NG");

const lagos = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
export const when = (iso: string) => lagos.format(new Date(iso));

export const short = (account: string) => account.slice(0, 6);

export function ago(minutes: number): string {
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

/** Minutes from `now` until `iso` (negative if already past). */
export const minutesUntil = (iso: string, now: string) =>
  (new Date(iso).getTime() - new Date(now).getTime()) / 60000;

export function duration(minutes: number): string {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), rest = m % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

export const HOLD_STATUS: Record<string, string> = {
  requested: "Waiting for bank", active: "Held", declined: "Nothing to hold",
  released: "Released", expired: "Lapsed",
};
export const HOLD_SOURCE: Record<string, string> = {
  auto_flag: "the scorer", agent: "the investigator", analyst: "an analyst",
};

/** A hold awaiting a person's decision: open, and not yet confirmed or released. */
export const awaitsDecision = (hold: Hold) =>
  (hold.status === "requested" || hold.status === "active") && !hold.review;

/** The reasons behind a hold, as a list. Scorer holds carry them in a fixed format. */
export function reasonsOf(hold: Hold): string[] {
  const prefix = "Flagged by the mule scorer: ";
  if (hold.reason.startsWith(prefix)) return hold.reason.slice(prefix.length).split("; ").filter(Boolean);
  return [hold.reason];
}

/** Stolen money traced into each account, summed over every case. */
export function tracedByAccount(cases: CaseRow[]): Map<string, number> {
  const traced = new Map<string, number>();
  for (const c of cases) {
    for (const [account, kobo] of Object.entries(c.holdings ?? {})) traced.set(account, (traced.get(account) ?? 0) + kobo);
  }
  return traced;
}

export type CaseState = { label: string; tone: "loose" | "window" | "review" | "done" };

/** The one thing a desk most needs to know about a case right now. */
export function caseState(c: CaseRow): CaseState {
  const loose = Math.max(c.in_accounts_kobo - c.held_kobo, 0);
  if (loose > 0) return { label: `${naira(loose)} not held`, tone: "loose" };
  if (c.minutes_since_report < 30) return { label: "In response window", tone: "window" };
  if (c.holds_awaiting_review > 0)
    return { label: `${c.holds_awaiting_review} ${c.holds_awaiting_review === 1 ? "hold" : "holds"} to review`, tone: "review" };
  return { label: c.in_accounts_kobo > 0 ? "All held" : "Nothing left to hold", tone: "done" };
}

export const needsAttention = (c: CaseRow) => caseState(c).tone !== "done";
