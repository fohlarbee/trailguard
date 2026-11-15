import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { runInvestigator } from "@/app/actions";
import { Submit } from "@/components/client";
import { HoldsTable, Legend, Split } from "@/components/parts";
import { Shell } from "@/components/Shell";
import { TrailGraph } from "@/components/TrailGraph";
import { api, ApiError, type CaseDetail } from "@/lib/api";
import { ago, naira, short, when } from "@/lib/format";

async function Case({ id }: { id: string }) {
  let d: CaseDetail;
  try {
    d = await api<CaseDetail>(`/v1/cases/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.message.startsWith("no case")) notFound();
    throw error;
  }
  const from = `/cases/${id}`;
  const loose = Math.max(d.in_accounts_kobo - d.held_kobo, 0);
  const left = 30 - d.minutes_since_report;
  const run = d.runs[0];

  return (
    <>
      <p className="crumb"><Link href="/">All cases</Link></p>
      <section className="lead">
        <h1>{naira(d.held_kobo)} held of {naira(d.stolen_kobo)} stolen</h1>
        <p>
          Case {d.case_id}, reported by {d.reporting_bank} {ago(d.minutes_since_report)} ({when(d.reported_at)}).{" "}
          {left > 0
            ? <strong className="urgent">{Math.ceil(left)} minutes left of the 30-minute response window.</strong>
            : "The 30-minute response window has passed."}
        </p>
        <Split stolen={d.stolen_kobo} held={d.held_kobo} inAccounts={d.in_accounts_kobo} gone={d.exited_kobo} />
        <Legend />
        <dl className="figures">
          <div><dt>Held</dt><dd>{naira(d.held_kobo)}</dd></div>
          <div><dt>In accounts, not held</dt><dd>{naira(loose)}</dd></div>
          <div><dt>Gone</dt><dd>{naira(d.exited_kobo)}</dd></div>
        </dl>
      </section>

      <section>
        <h2>Where the money went</h2>
        <TrailGraph detail={d} />
      </section>

      <section>
        <h2>Holds on this trail</h2>
        <p className="hint">Only the bank that holds an account can confirm or release a hold on it.</p>
        <HoldsTable holds={d.holds} from={from} />
      </section>

      <section>
        <h2>Accounts on the trail</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr><th>Account</th><th>Opened</th><th className="num">Stolen money here now</th><th>Why it was flagged</th></tr>
            </thead>
            <tbody>
              {d.accounts.map((a) => (
                <tr key={a.account}>
                  <td><code>{short(a.account)}</code> {a.bank_id}<small>KYC tier {a.kyc_tier}</small></td>
                  <td>{when(a.opened_at)}</td>
                  <td className="num">{naira(a.stolen_in_account_kobo)}</td>
                  <td>
                    {a.alert ? (
                      <>Flagged {when(a.alert.first_flagged_at)}<small>{a.alert.reasons.join("; ")}</small></>
                    ) : <span className="muted">Not flagged</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="agent">
        <h2>Investigator</h2>
        <form action={runInvestigator}>
          <input type="hidden" name="case_id" value={d.case_id} />
          <input type="hidden" name="from" value={from} />
          <Submit busy="Investigating, about a minute" className="primary">
            {run ? "Investigate again" : "Investigate this case"}
          </Submit>
          <span className="hint">
            Traces the money, holds what is still in accounts, and prepares the evidence pack and report draft.
          </span>
        </form>
        {run ? (
          <>
            <p className="hint">Last run finished as “{run.outcome ?? "unfinished"}”, working as of {when(run.as_of)}.</p>
            <div className="handover scroll"><Markdown remarkPlugins={[remarkGfm]}>{run.summary ?? ""}</Markdown></div>
            <details>
              <summary>{run.steps?.length ?? 0} actions taken</summary>
              <ol>
                {(run.steps ?? []).map((step, i) => (
                  <li key={i} className={step.failed ? "failed" : undefined}>
                    <code>{step.tool}</code>{step.failed && " (refused by the tool)"}
                  </li>
                ))}
              </ol>
            </details>
          </>
        ) : <p className="empty">The investigator has not worked this case yet.</p>}
      </section>

      <section>
        <h2>Paperwork</h2>
        {d.evidence ? (
          <p>Evidence pack {d.evidence.pack_id}, sealed {when(d.evidence.built_at)}.<small>SHA-256 <code>{d.evidence.hash}</code></small></p>
        ) : <p className="empty">No evidence pack yet. The investigator builds one.</p>}
        {d.str_draft ? (
          <details>
            <summary>Suspicious transaction report, draft {d.str_draft.draft_id} (not filed)</summary>
            <p className="narrative">{d.str_draft.body.narrative}</p>
          </details>
        ) : <p className="empty">No report draft yet.</p>}
      </section>
    </>
  );
}

export default async function Page({ params, searchParams }: PageProps<"/cases/[id]">) {
  const [{ id }, { error }] = await Promise.all([params, searchParams]);
  return <Shell error={typeof error === "string" ? error : undefined}><Case id={id} /></Shell>;
}
