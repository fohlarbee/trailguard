import Link from "next/link";
import { reduceHold } from "@/app/actions";
import type { CaseRow, Hold } from "@/lib/api";
import {
  HOLD_SOURCE, HOLD_STATUS, awaitsDecision, caseState, duration, minutesUntil, naira, reasonsOf, short, when,
} from "@/lib/format";
import { Decide, Submit } from "./client";

/** Where stolen money is right now: held, in accounts but not held, or gone. */
export function Split({ stolen, held, inAccounts, gone, large }: {
  stolen: number; held: number; inAccounts: number; gone: number; large?: boolean;
}) {
  const loose = Math.max(inAccounts - held, 0);
  const pct = (part: number) => `${stolen ? (part / stolen) * 100 : 0}%`;
  return (
    <span className={large ? "split large" : "split"} role="img"
          aria-label={`${naira(held)} held, ${naira(loose)} in accounts and not held, ${naira(gone)} gone`}>
      <i className="held" style={{ width: pct(held) }} />
      <i className="loose" style={{ width: pct(loose) }} />
      <i className="gone" style={{ width: pct(gone) }} />
    </span>
  );
}

/** The split's three parts with their amounts, doubling as its legend. */
export function SplitFigures({ held, inAccounts, gone }: { held: number; inAccounts: number; gone: number }) {
  return (
    <dl className="figures">
      <div><dt><i className="held" />Held</dt><dd>{naira(held)}</dd></div>
      <div><dt><i className="loose" />In accounts, not held</dt><dd>{naira(Math.max(inAccounts - held, 0))}</dd></div>
      <div><dt><i className="gone" />Gone</dt><dd>{naira(gone)}</dd></div>
    </dl>
  );
}

export function CaseStatus({ row }: { row: CaseRow }) {
  const state = caseState(row);
  return <span className={`state ${state.tone}`}>{state.label}</span>;
}

export function HoldStatus({ hold }: { hold: Hold }) {
  return <span className={`pill ${hold.status}`}>{HOLD_STATUS[hold.status] ?? hold.status}</span>;
}

/** When a hold lapses, or that it no longer will. */
export function Lapse({ hold, now }: { hold: Hold; now: string }) {
  if (hold.review === "confirmed") return <span className="muted">Confirmed, does not lapse</span>;
  if (!awaitsDecision(hold)) return <span className="muted">{when(hold.expires_at)}</span>;
  const left = minutesUntil(hold.expires_at, now);
  return (
    <span className={left < 120 ? "lapse soon" : "lapse"}>
      Lapses in {duration(left)}<small>{when(hold.expires_at)}</small>
    </span>
  );
}

/** How a hold compares with the stolen money traced into the account. */
export function Sizing({ held, traced, scope }: { held: number; traced: number; scope: "all" | "case" }) {
  if (traced <= 0) {
    return <p className="sizing none">
      {scope === "all" ? "No reported case has traced stolen money here yet." : "None of this case's money is still here."}
    </p>;
  }
  const extra = held - traced;
  return (
    <p className={extra > traced * 0.1 ? "sizing over" : "sizing"}>
      {naira(traced)} traced as stolen{scope === "case" ? " in this case" : ""}
      {extra > traced * 0.1 && <>. The other {naira(extra)} is not linked to {scope === "case" ? "this case" : "any case"}</>}.
    </p>
  );
}

/** The investigator's suggestion to hold less, and the button to accept it. */
export function Advice({ hold, from, canDecide }: { hold: Hold; from: string; canDecide: boolean }) {
  if (hold.recommended_kobo == null || hold.status !== "active") return null;
  return (
    <form action={reduceHold} className="advice">
      <input type="hidden" name="hold_id" value={hold.hold_id} />
      <input type="hidden" name="label" value={short(hold.account)} />
      <input type="hidden" name="from" value={from} />
      <p>
        The investigator recommends holding {naira(hold.recommended_kobo)} instead of {naira(hold.held_kobo)}.{" "}
        {hold.recommendation}
      </p>
      {hold.mine && (
        <Submit busy="Reducing" disabled={!canDecide}>Reduce to {naira(hold.recommended_kobo)}</Submit>
      )}
    </form>
  );
}

/** Holds on the desk's own accounts that a person must confirm or release, soonest to lapse first. */
export function DecisionQueue({ holds, now, from, canDecide, caseFor, traced }: {
  holds: Hold[]; now: string; from: string; canDecide: boolean; caseFor: (account: string) => string[];
  traced: Map<string, number>;
}) {
  return (
    <ol className="queue">
      {holds.map((hold) => {
        const left = minutesUntil(hold.expires_at, now);
        const amount = hold.held_kobo ?? hold.requested_kobo;
        const cases = caseFor(hold.account);
        return (
          <li key={hold.hold_id} className={left < 120 ? "soon" : undefined}>
            <div className="what">
              <p className="amount">{naira(amount)}</p>
              <p>
                {hold.status === "active" ? "held" : "asked for"} on <code>{short(hold.account)}</code>
                <small>
                  Placed by {HOLD_SOURCE[hold.source] ?? hold.source} {when(hold.placed_at)}
                  {hold.status === "requested" && ". The bank has not confirmed the amount yet"}
                </small>
              </p>
              <Sizing held={amount} traced={traced.get(hold.account) ?? 0} scope="all" />
            </div>
            <div className="why">
              <ul>{reasonsOf(hold).slice(0, 4).map((r) => <li key={r}>{r}</li>)}</ul>
              {cases.length > 0 && (
                <p className="cases">
                  On the trail of{" "}
                  {cases.map((id, i) => (
                    <span key={id}>{i > 0 && ", "}<Link href={`/cases/${id}#acct-${hold.account}`}>{id}</Link></span>
                  ))}
                </p>
              )}
              <Advice hold={hold} from={from} canDecide={canDecide} />
            </div>
            <div className="act">
              <p className="countdown">
                Lapses in <strong>{duration(left)}</strong>
                <small>{when(hold.expires_at)}</small>
              </p>
              <Decide holdId={hold.hold_id} label={short(hold.account)} amount={naira(amount)}
                      from={from} canDecide={canDecide} />
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** A record of holds, for the Holds page. */
export function HoldsTable({ holds, now, from, canDecide }: {
  holds: Hold[]; now: string; from: string; canDecide: boolean;
}) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Account</th><th>Status</th><th className="num">Asked</th><th className="num">Held</th>
            <th>Placed</th><th>Lapses</th><th>Decision</th>
          </tr>
        </thead>
        <tbody>
          {holds.map((hold) => (
            <tr key={hold.hold_id}>
              <td>
                <code>{short(hold.account)}</code> {hold.bank_id}
                <small>{hold.reason}</small>
                <Advice hold={hold} from={from} canDecide={canDecide} />
              </td>
              <td><HoldStatus hold={hold} /></td>
              <td className="num">{naira(hold.requested_kobo)}</td>
              <td className="num">{naira(hold.held_kobo)}</td>
              <td>{when(hold.placed_at)}<small>by {HOLD_SOURCE[hold.source] ?? hold.source}</small></td>
              <td><Lapse hold={hold} now={now} /></td>
              <td>
                {hold.review
                  ? <>{hold.review === "confirmed" ? "Confirmed" : "Released"}<small>by {hold.reviewed_by}</small></>
                  : awaitsDecision(hold)
                    ? <Decide holdId={hold.hold_id} label={short(hold.account)}
                              amount={naira(hold.held_kobo ?? hold.requested_kobo)} from={from} canDecide={canDecide} />
                    : <span className="muted">No decision</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Tabs that filter a list through the address, so a filtered view can be shared or reloaded. */
export function Tabs({ tabs, active, label }: {
  tabs: { key: string; label: string; count: number; href: string }[]; active: string; label: string;
}) {
  return (
    <nav className="tabs" aria-label={label}>
      {tabs.map((tab) => (
        <Link key={tab.key} href={tab.href} aria-current={tab.key === active ? "page" : undefined}>
          {tab.label} <span className="count">{tab.count}</span>
        </Link>
      ))}
    </nav>
  );
}
