import Link from "next/link";
import { HoldsTable, Legend, Split } from "@/components/parts";
import { Shell } from "@/components/Shell";
import { api, type CaseRow, type Hold } from "@/lib/api";
import { ago, naira, when } from "@/lib/format";

const AGENT: Record<string, string> = {
  completed: "Investigated", refused: "Declined by model", failed: "Run failed", stopped: "Run cut short",
};

async function Desk() {
  const [cases, holds] = await Promise.all([api<CaseRow[]>("/v1/cases"), api<Hold[]>("/v1/holds")]);
  const waiting = holds
    .filter((h) => (h.status === "requested" || h.status === "active") && !h.review)
    .map((h) => ({ ...h, mine: true }));
  const sum = (key: "stolen_kobo" | "held_kobo" | "in_accounts_kobo" | "exited_kobo") =>
    cases.reduce((total, c) => total + c[key], 0);
  const stolen = sum("stolen_kobo"), held = sum("held_kobo"), inAccounts = sum("in_accounts_kobo");

  return (
    <>
      <section className="lead">
        <h1>{naira(held)} held of {naira(stolen)} reported stolen</h1>
        <p>
          {cases.length} cases so far. {naira(Math.max(inAccounts - held, 0))} is still in accounts
          without a hold, and {naira(sum("exited_kobo"))} has left the banking system.
        </p>
        <Split stolen={stolen} held={held} inAccounts={inAccounts} gone={sum("exited_kobo")} />
        <Legend />
      </section>

      <section>
        <h2>Holds waiting for your decision</h2>
        <p className="hint">
          Each hold lapses at the time shown unless you confirm it. Release it if the account holder is not involved.
        </p>
        {waiting.length ? <HoldsTable holds={waiting} from="/" showCase />
          : <p className="empty">Nothing is waiting on this desk.</p>}
      </section>

      <section>
        <h2>Cases</h2>
        {cases.length === 0 ? <p className="empty">No fraud has been reported yet.</p> : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Case</th><th>Reported</th><th className="num">Stolen</th><th>Where the money is</th>
                  <th className="num">Held</th><th className="num">Gone</th><th>Investigator</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr key={c.case_id}>
                    <td><Link href={`/cases/${c.case_id}`}>{c.case_id}</Link></td>
                    <td>{ago(c.minutes_since_report)}<small>{when(c.reported_at)}</small></td>
                    <td className="num">{naira(c.stolen_kobo)}</td>
                    <td><Split stolen={c.stolen_kobo} held={c.held_kobo} inAccounts={c.in_accounts_kobo} gone={c.exited_kobo} /></td>
                    <td className="num">{naira(c.held_kobo)}</td>
                    <td className="num">{naira(c.exited_kobo)}</td>
                    <td>{c.agent_outcome ? AGENT[c.agent_outcome] ?? c.agent_outcome : <span className="muted">Not run</span>}</td>
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
  const error = (await searchParams).error;
  return <Shell error={typeof error === "string" ? error : undefined}><Desk /></Shell>;
}
