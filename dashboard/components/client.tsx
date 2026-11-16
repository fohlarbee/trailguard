"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useFormStatus } from "react-dom";
import { reviewHold, startDemo } from "@/app/actions";

/** Re-reads the page's data on a timer, so new cases and bank answers appear unprompted. */
export function AutoRefresh({ seconds }: { seconds: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => {
      // Never refresh under someone who is in the middle of something.
      const busy = document.querySelector("[data-pending='true'], [data-confirming='true']");
      const typing = document.activeElement?.tagName === "INPUT";
      if (!busy && !typing && document.visibilityState === "visible") router.refresh();
    }, seconds * 1000);
    return () => clearInterval(timer);
  }, [router, seconds]);
  return null;
}

export function Submit({ children, busy, className, name, value, disabled }: {
  children: React.ReactNode; busy: string; className?: string; name?: string; value?: string; disabled?: boolean;
}) {
  const { pending, data } = useFormStatus();
  const mine = pending && (!name || data?.get(name) === value);
  return (
    <button type="submit" className={className} name={name} value={value}
            disabled={pending || disabled} data-pending={mine ? "true" : undefined}>
      {mine ? busy : children}
    </button>
  );
}

export function DeskSelect({ desks, current }: { desks: string[]; current: string }) {
  return (
    <select name="desk" defaultValue={current} aria-label="Fraud desk"
            onChange={(event) => event.currentTarget.form?.requestSubmit()}>
      {desks.map((desk) => <option key={desk} value={desk}>{desk}</option>)}
    </select>
  );
}

/** A confirmation or error after an action. Removed from the address so a reload does not repeat it. */
export function Flash({ kind, message }: { kind: "done" | "error"; message: string }) {
  const [open, setOpen] = useState(true);
  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete("done");
    url.searchParams.delete("error");
    window.history.replaceState(window.history.state, "", url);
  }, [message]);
  if (!open) return null;
  return (
    <div className={`flash ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <p>{message}</p>
      <button type="button" className="quiet" onClick={() => setOpen(false)}>Dismiss</button>
    </div>
  );
}

/**
 * Confirm or release a hold. Releasing frees money that may be stolen and cannot be
 * undone, so it asks once more and names the amount.
 */
export function Decide({ holdId, label, amount, from, canDecide }: {
  holdId: number; label: string; amount: string; from: string; canDecide: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <form action={reviewHold} className="decide" data-confirming={confirming ? "true" : undefined}>
      <input type="hidden" name="hold_id" value={holdId} />
      <input type="hidden" name="label" value={label} />
      <input type="hidden" name="from" value={from} />
      {confirming ? (
        <>
          <p className="ask">Release {amount} on {label}? This cannot be undone.</p>
          <Submit name="decision" value="release" busy="Releasing" className="danger">Release {amount}</Submit>
          <button type="button" className="quiet" onClick={() => setConfirming(false)}>Keep the hold</button>
        </>
      ) : (
        <>
          <Submit name="decision" value="confirm" busy="Confirming" className="primary" disabled={!canDecide}>
            Confirm hold
          </Submit>
          <button type="button" disabled={!canDecide} onClick={() => setConfirming(true)}>Release</button>
        </>
      )}
    </form>
  );
}

/** Start or restart the demo. It wipes the data on screen, so it asks first. */
export function StartDemo({ label }: { label: string }) {
  const [confirming, setConfirming] = useState(false);
  return (
    <form action={startDemo} className="decide" data-confirming={confirming ? "true" : undefined}>
      {confirming ? (
        <>
          <p className="ask wide">This clears every case and hold on screen and plays the theft from the start.</p>
          <Submit busy="Starting" className="primary">Start the demo</Submit>
          <button type="button" className="quiet" onClick={() => setConfirming(false)}>Cancel</button>
        </>
      ) : (
        <button type="button" className="primary" onClick={() => setConfirming(true)}>{label}</button>
      )}
    </form>
  );
}
