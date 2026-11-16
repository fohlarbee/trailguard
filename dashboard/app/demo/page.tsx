import Link from "next/link";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { openDesk } from "@/app/actions";
import { StartDemo, Submit } from "@/components/client";
import { Ledger } from "@/components/parts";
import { Shell, messageFrom } from "@/components/Shell";
import { TrailGraph } from "@/components/TrailGraph";
import { api, type CaseDetail, type Demo, type DemoItem } from "@/lib/api";
import { awaitsDecision, naira } from "@/lib/format";

const clockTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const RUNNING = ["preparing", "playing", "investigating"];

function pace(d: Demo): string {
  if (d.phase === "investigating") return "Real time while the investigator works";
  if (d.phase !== "playing") return "Real time";
  return `${d.rate}× speed`;
}

/** Where the story is, worked out from what TrailGuard has seen so far. */
function story(d: Demo) {
  const s = d.scenario;
  const stolen = s.theft_at ? new Date(d.now) >= new Date(s.theft_at) : false;
  const flagged = d.feed.some((item) => item.tone === "scorer");
  const reported = d.case_id !== null;
  const stages = [
    { label: "Money stolen", done: stolen },
    { label: "Mules flagged and held", done: flagged },
    { label: "Theft reported", done: reported },
    { label: "Investigator at work", done: d.agent_outcome !== null },
    { label: "Fraud desks decide", done: d.phase === "finished" },
  ];
  const how = s.method === "social_engineering"
    ? `A ${s.victim_bank} customer has just been tricked into sending ${naira(s.stolen_kobo)} to a fraudster.`
    : `A ${s.victim_bank} customer's account has just been taken over, and ${naira(s.stolen_kobo)} sent out of it.`;
  let line: string;
  if (d.phase === "preparing") line = "Loading a day of ordinary activity at six banks and fintechs.";
  else if (d.phase === "failed") line = "The demo stopped with an error.";
  else if (d.phase === "finished") line = "The money is held. What happens to it now is up to the fraud desks of the banks holding it.";
  else if (d.phase === "investigating") line = `${d.case_id?.split("-")[0]} has reported the theft. The AI investigator is working the case.`;
  else if (reported) line = "The investigator has handed over. The demo plays on until the money has nowhere left to go.";
  else if (flagged) line = "TrailGuard has spotted the mule accounts and the banks have frozen the money. The victim has not reported anything yet.";
  else if (stolen) line = how;
  else line = "An ordinary night across six institutions. TrailGuard scores every payment as it arrives.";
  return { stages, line };
}

function Steps({ steps, working }: { steps: string[]; working: boolean }) {
  // While the investigator works, its latest steps show as they happen; afterwards they fold away.
  if (working) return <ol className="steps" start={Math.max(steps.length - 4, 0) + 1}>{steps.slice(-4).map((s, i) => <li key={i}>{s}</li>)}</ol>;
  return (
    <details className="steps-all">
      <summary>The {steps.length} steps it took</summary>
      <ol className="steps">{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
    </details>
  );
}

function Timeline({ items }: { items: DemoItem[] }) {
  if (items.length === 0) {
    return <p className="empty">Nothing unusual yet. Every payment is being scored as it arrives.</p>;
  }
  return (
    <table className="timeline">
      <thead><tr><th>Time</th><th>Who</th><th>What happened</th></tr></thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.id}>
            <td className="num">{item.at ? clockTime.format(new Date(item.at)) : ""}</td>
            <td className={`who ${item.tone}`}>{item.actor}</td>
            <td>
              <p className="what">{item.title}</p>
              {item.detail && <small>{item.detail}</small>}
              {item.steps && item.steps.length > 0 && (
                <Steps steps={item.steps} working={item.title === "Working the case"} />
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

async function DemoView() {
  const d = await api<Demo>("/v1/demo");
  const detail = d.case_id ? await api<CaseDetail>(`/v1/cases/${d.case_id}`) : null;
  const running = RUNNING.includes(d.phase);

  if (d.phase === "idle") {
    return (
      <section className="lead demo-intro">
        <h1>Live demo</h1>
        <p>
          Play one theft from start to finish on synthetic data. A customer is scammed, the money moves through
          mule accounts at several banks, and TrailGuard flags, freezes and investigates it while you watch.
        </p>
        <p>
          It takes about two minutes. The clock runs fast while money moves and drops to real time while the
          AI investigator works, so its speed is shown as it really is.
        </p>
        <StartDemo label="Start the demo" />
      </section>
    );
  }

  const { stages, line } = story(d);
  const current = stages.findIndex((stage) => !stage.done);
  const waiting = detail ? [...new Set(detail.holds.filter(awaitsDecision).map((h) => h.bank_id))].sort() : [];

  return (
    <>
      <section className="lead">
        <div className="demo-title">
          <h1>
            {d.scenario.stolen_kobo
              ? `${naira(d.scenario.stolen_kobo)} stolen from a ${d.scenario.victim_bank} customer`
              : "Live demo"}
          </h1>
          {!running && <StartDemo label="Play it again" />}
        </div>
        <p aria-live="polite">{line}</p>
        <p className="demo-clock">
          <time dateTime={d.now}>{clockTime.format(new Date(d.now))}</time>
          <span>WAT, simulated. <strong className={d.phase === "investigating" ? "live" : undefined}>{pace(d)}</strong></span>
        </p>
        {d.error && <p className="flash error" role="alert">{d.error}</p>}
        {detail && (
          <>
            <Ledger stolen={detail.stolen_kobo} held={detail.held_kobo}
                    inAccounts={detail.in_accounts_kobo} gone={detail.exited_kobo} />
          </>
        )}
      </section>

      <ol className="route" aria-label="Progress">
        {stages.map((stage, i) => (
          <li key={stage.label} className={stage.done ? "done" : i === current && running ? "now" : undefined}
              aria-current={i === current ? "step" : undefined}>
            {stage.label}
          </li>
        ))}
      </ol>

      <div className="demo-grid">
        <section aria-labelledby="timeline">
          <h2 id="timeline">What TrailGuard sees</h2>
          <p className="hint">Newest first. Only what the banks&apos; connectors report reaches TrailGuard.</p>
          <Timeline items={d.feed} />
        </section>

        <section aria-labelledby="money">
          <h2 id="money">Where the money went</h2>
          {detail ? (
            <>
              <p className="hint">Traced across banks from the victim&apos;s report.</p>
              <TrailGraph detail={detail} />
              <div className="demo-actions">
                <Link href={`/cases/${detail.case_id}`} className="button">Open the case</Link>
                {waiting.map((bank) => (
                  <form key={bank} action={openDesk}>
                    <input type="hidden" name="desk" value={bank} />
                    <input type="hidden" name="to" value="/" />
                    <Submit busy="Opening">Decide {bank}&apos;s holds</Submit>
                  </form>
                ))}
              </div>
            </>
          ) : (
            <p className="empty">
              There is no trail to follow until someone reports a theft. Until then, the scorer catches mule
              accounts on its own, from how they behave.
            </p>
          )}
        </section>
      </div>

      {detail?.runs[0]?.summary && (
        <section className="handover demo-handover" aria-labelledby="handover">
          <h2 id="handover">The investigator&apos;s handover</h2>
          <p className="hint">Written by the AI investigator for the fraud desks. It recommends; only people at the banks decide.</p>
          <div className="scroll"><Markdown remarkPlugins={[remarkGfm]}>{detail.runs[0].summary}</Markdown></div>
        </section>
      )}
    </>
  );
}

export default async function DemoPage({ searchParams }: PageProps<"/demo">) {
  const message = messageFrom(await searchParams);
  let running = false;
  try {
    running = RUNNING.includes((await api<Demo>("/v1/demo")).phase);
  } catch {
    // The shell reports an unreachable API.
  }
  return (
    <Shell active="demo" message={message} refresh={running ? 2 : 15}>
      <DemoView />
    </Shell>
  );
}
