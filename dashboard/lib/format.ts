export const naira = (kobo: number | null | undefined) =>
  kobo == null ? "–" : "₦" + Math.round(kobo / 100).toLocaleString("en-NG");

const lagos = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
export const when = (iso: string) => lagos.format(new Date(iso));

export const short = (account: string) => account.slice(0, 6);

export function ago(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

export const HOLD_STATUS: Record<string, string> = {
  requested: "Waiting for bank", active: "Held", declined: "Nothing left to hold",
  released: "Released", expired: "Lapsed",
};
export const HOLD_SOURCE: Record<string, string> = {
  auto_flag: "Scorer", agent: "Investigator", analyst: "Analyst",
};
