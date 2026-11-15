import { reduceHold, reviewHold } from "@/app/actions";
import type { Hold } from "@/lib/api";
import { HOLD_SOURCE, HOLD_STATUS, naira, short, when } from "@/lib/format";
import { Submit } from "./client";

/** Where a case's stolen money is right now: held, in accounts but not held, or gone. */
export function Split({ stolen, held, inAccounts, gone }: {
  stolen: number; held: number; inAccounts: number; gone: number;
}) {
  const loose = Math.max(inAccounts - held, 0);
  const pct = (part: number) => `${stolen ? (part / stolen) * 100 : 0}%`;
  return (
    <span className="split" role="img"
          aria-label={`${naira(held)} held, ${naira(loose)} in accounts and not held, ${naira(gone)} gone`}>
      <i className="held" style={{ width: pct(held) }} />
      <i className="loose" style={{ width: pct(loose) }} />
      <i className="gone" style={{ width: pct(gone) }} />
    </span>
  );
}

export function Legend() {
  return (
    <p className="legend">
      <span><i className="held" />Held</span>
      <span><i className="loose" />In accounts, not held</span>
      <span><i className="gone" />Gone</span>
    </p>
  );
}

export function HoldsTable({ holds, from, showCase }: { holds: Hold[]; from: string; showCase?: boolean }) {
  if (!holds.length) return <p className="empty">No holds yet.</p>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Account</th><th>Status</th><th className="num">Asked</th><th className="num">Held</th>
            <th>Placed by</th><th>Lapses</th><th>Decision</th>
          </tr>
        </thead>
        <tbody>
          {holds.map((hold) => {
            const open = hold.status === "requested" || hold.status === "active";
            return (
              <tr key={hold.hold_id}>
                <td>
                  <code>{short(hold.account)}</code> {hold.bank_id}
                  {showCase && hold.case_id && <> <a href={`/cases/${hold.case_id}`}>case</a></>}
                  <small>{hold.reason}</small>
                  {hold.recommended_kobo != null && hold.status === "active" && (
                    <form action={reduceHold} className="advice">
                      <input type="hidden" name="hold_id" value={hold.hold_id} />
                      <input type="hidden" name="from" value={from} />
                      <p>
                        The investigator recommends holding {naira(hold.recommended_kobo)} instead
                        of {naira(hold.held_kobo)}. {hold.recommendation}
                      </p>
                      {hold.mine && <Submit busy="Reducing">Reduce to {naira(hold.recommended_kobo)}</Submit>}
                    </form>
                  )}
                </td>
                <td><span className={`pill ${hold.status}`}>{HOLD_STATUS[hold.status] ?? hold.status}</span></td>
                <td className="num">{naira(hold.requested_kobo)}</td>
                <td className="num">{naira(hold.held_kobo)}</td>
                <td>{HOLD_SOURCE[hold.source] ?? hold.source}<small>{when(hold.placed_at)}</small></td>
                <td>{hold.review === "confirmed" ? "Does not lapse" : when(hold.expires_at)}</td>
                <td>
                  {hold.review ? (
                    <>{hold.review === "confirmed" ? "Confirmed" : "Released"}<small>by {hold.reviewed_by}</small></>
                  ) : open && hold.mine ? (
                    <form action={reviewHold} className="decide">
                      <input type="hidden" name="hold_id" value={hold.hold_id} />
                      <input type="hidden" name="from" value={from} />
                      <Submit name="decision" value="confirm" busy="Confirming" className="primary">Confirm hold</Submit>
                      <Submit name="decision" value="release" busy="Releasing">Release</Submit>
                    </form>
                  ) : open ? (
                    <span className="muted">Waiting for {hold.bank_id}</span>
                  ) : (
                    <span className="muted">Not reviewed</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
