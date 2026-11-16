import Link from "next/link";
import { CaseStatus, DecisionQueue, Ledger, Split, Tabs } from "@/components/parts";
import { Shell, messageFrom } from "@/components/Shell";
import { api, me, reviewerName, type CaseRow, type Hold } from "@/lib/api";
import {
  ago, awaitsDecision, duration, minutesUntil, naira, needsAttention, tracedByAccount, when,
} from "@/lib/format";

const INVESTIGATOR: Record<string, string> = {
  completed: "Investigated", refused: "Declined", failed: "Failed", stopped: "Cut short",
};
const VIEWS = ["attention", "mine", "all"] as const;
type View = (typeof VIEWS)[number];

async function Desk({ view }: { view: View }) {
  const [clock, cases, holds, reviewer] = await Promise.all([
    me(), api<CaseRow[]>("/v1/cases"), api<Hold[]>("/v1/holds"), reviewerName(),
  ]);
  const desk = clock.bank;
  const waiting = holds.filter(awaitsDecision).map((h) => ({ ...h, mine: true }))
    .sort((a, b) => a.expires_at.localeCompare(b.expires_at));
  const caseFor = (account: string) =>
    cases.filter((c) => c.trail_accounts?.includes(account)).map((c) => c.case_id);

  const total = (key: "stolen_kobo" | "held_kobo" | "in_accounts_kobo" | "exited_kobo") =>
    cases.reduce((sum, c) => sum + c[key], 0);
  const filters: Record<View, (c: CaseRow) => boolean> = {
    attention: needsAttention,
    mine: (c) => c.reporting_bank === desk || !!c.banks_involved?.includes(desk),
    all: () => true,
  };
  const shown = cases.filter(filters[view]);
  const soonest = waiting[0] && minutesUntil(waiting[0].expires_at, clock.now);

  return (
    <>
      <section className="lead" aria-labelledby="queue-title">
        <h1 id="queue-title">
          {waiting.length === 0 ? "Nothing needs your decision"
            : `${waiting.length} ${waiting.length === 1 ? "hold needs" : "holds need"} your decision`}
        </h1>
        <p>
          {waiting.length === 0
            ? `New holds on ${desk} accounts appear here as soon as the scorer or the investigator places them.`
            : `These holds are on ${desk} accounts. The first lapses in ${duration(soonest)}. ` +
              "Confirm a hold to keep it, or release it if the account holder is not involved."}
        </p>
        {waiting.length > 0 && (
          <DecisionQueue holds={waiting} now={clock.now} from="/" canDecide={!!reviewer} caseFor={caseFor}
                         traced={tracedByAccount(cases)} />
        )}
      </section>

      <section aria-labelledby="network-title">
        <h2 id="network-title">Stolen money across the network</h2>
        <p className="hint">
          Every reported case, at every bank, as of now. {cases.length} {cases.length === 1 ? "case" : "cases"} so far.
        </p>
        <Ledger stolen={total("stolen_kobo")} held={total("held_kobo")}
                inAccounts={total("in_accounts_kobo")} gone={total("exited_kobo")} />
      </section>

      <section aria-labelledby="cases-title">
        <div className="section-head">
          <h2 id="cases-title">Cases</h2>
          <Tabs label="Filter cases" active={view} tabs={[
            { key: "attention", label: "Needs attention", count: cases.filter(filters.attention).length, href: "/?view=attention" },
            { key: "mine", label: `Involves ${desk}`, count: cases.filter(filters.mine).length, href: "/?view=mine" },
            { key: "all", label: "All", count: cases.length, href: "/?view=all" },
          ]} />
        </div>
        {shown.length === 0 ? (
          <p className="empty">
            {view === "attention" ? "No case has money left to hold or a hold awaiting review."
              : view === "mine" ? `No reported fraud has passed through ${desk} yet.`
              : "No fraud has been reported yet."}
          </p>
        ) : (
          <div className="scroll">
            <table className="rows">
              <thead>
                <tr>
                  <th>Case</th><th>Reported</th><th className="num">Stolen</th><th>Now</th>
                  <th>Where the money is</th><th>Investigator</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => (
                  <tr key={c.case_id}>
                    <td><Link className="row-link" href={`/cases/${c.case_id}`}>{c.case_id}</Link></td>
                    <td>{ago(c.minutes_since_report)}<small>{when(c.reported_at)}</small></td>
                    <td className="num" data-label="Stolen">{naira(c.stolen_kobo)}</td>
                    <td><CaseStatus row={c} /></td>
                    <td className="bar">
                      <Split stolen={c.stolen_kobo} held={c.held_kobo} inAccounts={c.in_accounts_kobo} gone={c.exited_kobo} />
                      <small>{naira(c.held_kobo)} held, {naira(c.exited_kobo)} gone</small>
                    </td>
                    <td data-label="Investigator">{c.agent_outcome ? INVESTIGATOR[c.agent_outcome] ?? c.agent_outcome : <span className="muted">Not run</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

export default async function Page({ searchParams }: PageProps<"/">) {
  const params = await searchParams;
  const view = VIEWS.includes(params.view as View) ? (params.view as View) : "attention";
  return <Shell active="desk" message={messageFrom(params)}><Desk view={view} /></Shell>;
}
