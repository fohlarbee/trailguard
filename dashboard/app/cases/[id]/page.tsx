import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { runInvestigator } from "@/app/actions";
import { Decide, Submit } from "@/components/client";
import { Advice, HoldStatus, Lapse, Ledger, Reasons, Sizing } from "@/components/parts";
import { Shell, messageFrom } from "@/components/Shell";
import { TrailGraph } from "@/components/TrailGraph";
import { api, ApiError, reviewerName, type CaseDetail, type Hold } from "@/lib/api";
import { ago, awaitsDecision, duration, naira, short, when } from "@/lib/format";

const RUN_OUTCOME: Record<string, string> = {
  completed: "finished", refused: "was declined by the model", stopped: "stopped before finishing",
  failed: "could not run",
};

/** The hold that matters for an account: an open one if any, otherwise the latest. */
function holdFor(holds: Hold[], account: string): { current?: Hold; earlier: number } {
  const own = holds.filter((h) => h.account === account).sort((a, b) => b.placed_at.localeCompare(a.placed_at));
  const current = own.find((h) => h.status === "requested" || h.status === "active") ?? own[0];
  return { current, earlier: own.length - (current ? 1 : 0) };
}

async function Case({ id }: { id: string }) {
  let d: CaseDetail;
  try {
    d = await api<CaseDetail>(`/v1/cases/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.message.startsWith("no case")) notFound();
    throw error;
  }
  const reviewer = await reviewerName();
  const desk = d.holds.find((h) => h.mine)?.bank_id;
  const from = `/cases/${id}`;
  const left = 30 - d.minutes_since_report;
  const run = d.runs[0];
  const mineWaiting = d.holds.filter((h) => h.mine && awaitsDecision(h));

  return (
    <>
      <p className="crumb"><Link href="/">All cases</Link></p>
      <section className="lead">
        <h1>{naira(d.held_kobo)} held of {naira(d.stolen_kobo)} stolen</h1>
        <p>
          Case {d.case_id}, reported by {d.reporting_bank} {ago(d.minutes_since_report)}, at {when(d.reported_at)}.
        </p>
        <p className={left > 0 ? "window open" : "window"}>
          {left > 0
            ? <><strong>{duration(left)}</strong> left of the 30-minute response window</>
            : "The 30-minute response window has passed"}
        </p>
        <Ledger stolen={d.stolen_kobo} held={d.held_kobo} inAccounts={d.in_accounts_kobo} gone={d.exited_kobo} />
        {mineWaiting.length > 0 && (
          <p className="callout">
            {mineWaiting.length} {mineWaiting.length === 1 ? "hold on this trail is" : "holds on this trail are"} on{" "}
            {desk} accounts and {mineWaiting.length === 1 ? "needs" : "need"} your decision.{" "}
            <a href="#accounts">Go to the accounts</a>
          </p>
        )}
      </section>

      <nav className="jump" aria-label="Sections of this case">
        <a href="#trail">Trail</a>
        <a href="#accounts">Accounts and holds</a>
        <a href="#investigator">Investigator</a>
        <a href="#paperwork">Paperwork</a>
      </nav>

      <section id="trail" aria-labelledby="trail-title">
        <h2 id="trail-title">Where the money went</h2>
        <p className="hint">Select an account to see its flags and hold.</p>
        <TrailGraph detail={d} />
      </section>

      <section id="accounts" aria-labelledby="accounts-title">
        <h2 id="accounts-title">Accounts and holds</h2>
        <p className="hint">
          Each account the stolen money passed through. Only the bank that holds an account can confirm,
          release or reduce a hold on it.
        </p>
        {d.accounts.length === 0 ? <p className="empty">The money has not reached any account yet.</p> : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Account</th><th className="num">Stolen money here</th><th>Why it was flagged</th>
                  <th>Hold</th><th>Decision</th>
                </tr>
              </thead>
              <tbody>
                {d.accounts.map((a) => {
                  const { current: hold, earlier } = holdFor(d.holds, a.account);
                  return (
                    <tr key={a.account} id={`acct-${a.account}`}>
                      <td>
                        <code>{short(a.account)}</code> {a.bank_id}
                        <small>Opened {when(a.opened_at)}</small>
                        <small>KYC tier {a.kyc_tier}</small>
                      </td>
                      <td className="num">{naira(a.stolen_in_account_kobo)}</td>
                      <td>
                        {a.alert ? (
                          <>Flagged {when(a.alert.first_flagged_at)}
                            <Reasons reasons={a.alert.reasons} /></>
                        ) : <span className="muted">Not flagged</span>}
                      </td>
                      <td>
                        {hold ? (
                          <>
                            <HoldStatus hold={hold} /> {naira(hold.held_kobo ?? hold.requested_kobo)}
                            <small><Lapse hold={hold} now={d.now} /></small>
                            {(hold.status === "active" || hold.status === "requested") && (
                              <Sizing held={hold.held_kobo ?? hold.requested_kobo}
                                      traced={a.stolen_in_account_kobo} scope="case" />
                            )}
                            {earlier > 0 && <small>{earlier} earlier {earlier === 1 ? "hold" : "holds"}</small>}
                            <Advice hold={hold} from={`${from}#acct-${a.account}`} canDecide={!!reviewer} />
                          </>
                        ) : <span className="muted">No hold</span>}
                      </td>
                      <td>
                        {!hold ? null
                          : hold.review
                            ? <>{hold.review === "confirmed" ? "Confirmed" : "Released"}<small>by {hold.reviewed_by}</small></>
                            : awaitsDecision(hold)
                              ? hold.mine
                                ? <Decide holdId={hold.hold_id} label={short(hold.account)}
                                          amount={naira(hold.held_kobo ?? hold.requested_kobo)}
                                          from={`${from}#acct-${a.account}`} canDecide={!!reviewer} />
                                : <span className="muted">For {hold.bank_id} to decide</span>
                              : <span className="muted">No decision</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section id="investigator" aria-labelledby="investigator-title">
        <h2 id="investigator-title">Investigator</h2>
        <form action={runInvestigator} className="run">
          <input type="hidden" name="case_id" value={d.case_id} />
          <input type="hidden" name="from" value={`${from}#investigator`} />
          <Submit busy="Investigating. This takes about a minute" className="primary">
            {run ? "Investigate again" : "Investigate this case"}
          </Submit>
          <p className="hint">
            Traces the money as it stands now, holds what is still in accounts, chases banks, and prepares the
            evidence pack and report draft. It cannot confirm, release or file anything.
          </p>
        </form>
        {run ? (
          <article className="handover">
            <header>
              <p>
                Handover from the run at {when(run.as_of)}, which {RUN_OUTCOME[run.outcome ?? ""] ?? "did not finish"}.
              </p>
            </header>
            <div className="scroll"><Markdown remarkPlugins={[remarkGfm]}>{run.summary ?? ""}</Markdown></div>
            <details>
              <summary>The {run.steps?.length ?? 0} actions it took</summary>
              <ol>
                {(run.steps ?? []).map((step, i) => (
                  <li key={i} className={step.failed ? "failed" : undefined}>
                    <code>{step.tool}</code>{step.failed && " was refused by the tool"}
                  </li>
                ))}
              </ol>
            </details>
          </article>
        ) : <p className="empty">The investigator has not worked this case yet.</p>}
      </section>

      <section id="paperwork" aria-labelledby="paperwork-title">
        <h2 id="paperwork-title">Paperwork</h2>
        <dl className="papers">
          <div>
            <dt>Evidence pack</dt>
            <dd>
              {d.evidence ? (
                <>Pack {d.evidence.pack_id}, sealed {when(d.evidence.built_at)}
                  <small>SHA-256 <code>{d.evidence.hash}</code></small></>
              ) : <span className="muted">Not built yet. The investigator builds one.</span>}
            </dd>
          </div>
          <div>
            <dt>Suspicious transaction report</dt>
            <dd>
              {d.str_draft ? (
                <details>
                  <summary>Draft {d.str_draft.draft_id}, written {when(d.str_draft.drafted_at)}. Not filed.</summary>
                  <p className="narrative">{d.str_draft.body.narrative}</p>
                </details>
              ) : <span className="muted">Not drafted yet. The investigator drafts one.</span>}
            </dd>
          </div>
        </dl>
      </section>
    </>
  );
}

export default async function Page({ params, searchParams }: PageProps<"/cases/[id]">) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  return <Shell active="desk" message={messageFrom(query)}><Case id={id} /></Shell>;
}
