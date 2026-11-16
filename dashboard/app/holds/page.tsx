import { DecisionQueue, HoldsTable, Tabs } from "@/components/parts";
import { Shell, messageFrom } from "@/components/Shell";
import { api, me, reviewerName, type CaseRow, type Hold } from "@/lib/api";
import { awaitsDecision, tracedByAccount } from "@/lib/format";

const VIEWS = {
  waiting: { label: "Waiting for a decision", test: awaitsDecision,
             empty: "No hold is waiting for a decision." },
  confirmed: { label: "Confirmed", test: (h: Hold) => h.review === "confirmed",
               empty: "No hold has been confirmed yet." },
  lapsed: { label: "Lapsed", test: (h: Hold) => h.status === "expired",
            empty: "No hold has lapsed." },
  released: { label: "Released", test: (h: Hold) => h.status === "released",
              empty: "No hold has been released." },
  nothing: { label: "Nothing to hold", test: (h: Hold) => h.status === "declined",
             empty: "Every hold so far found money to hold." },
  all: { label: "All", test: () => true, empty: "No hold has been placed on this desk's accounts." },
} as const;
type View = keyof typeof VIEWS;

async function Holds({ view }: { view: View }) {
  const [clock, holds, cases, reviewer] = await Promise.all([
    me(), api<Hold[]>("/v1/holds"), api<CaseRow[]>("/v1/cases"), reviewerName(),
  ]);
  const mine = holds.map((h) => ({ ...h, mine: true })).sort((a, b) => b.placed_at.localeCompare(a.placed_at));
  const shown = mine.filter(VIEWS[view].test);
  const from = `/holds?view=${view}`;
  const caseFor = (account: string) =>
    cases.filter((c) => c.trail_accounts?.includes(account)).map((c) => c.case_id);

  return (
    <>
      <section className="lead">
        <h1>Holds on {clock.bank} accounts</h1>
        <p>
          Every hold placed on this desk&apos;s accounts, by the scorer, the investigator or an analyst.
          A hold lapses after 24 hours unless someone here confirms it.
        </p>
      </section>
      <section>
        <Tabs label="Filter holds" active={view} tabs={(Object.keys(VIEWS) as View[]).map((key) => ({
          key, label: VIEWS[key].label, count: mine.filter(VIEWS[key].test).length, href: `/holds?view=${key}`,
        }))} />
        {shown.length === 0 ? <p className="empty">{VIEWS[view].empty}</p>
          : view === "waiting"
            ? <DecisionQueue holds={[...shown].sort((a, b) => a.expires_at.localeCompare(b.expires_at))}
                             now={clock.now} from={from} canDecide={!!reviewer} caseFor={caseFor}
                             traced={tracedByAccount(cases)} />
            : <HoldsTable holds={shown} now={clock.now} from={from} canDecide={!!reviewer} />}
      </section>
    </>
  );
}

export default async function Page({ searchParams }: PageProps<"/holds">) {
  const params = await searchParams;
  const view = (params.view as string) in VIEWS ? (params.view as View) : "waiting";
  return <Shell active="holds" message={messageFrom(params)}><Holds view={view} /></Shell>;
}
