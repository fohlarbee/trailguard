import Link from "next/link";
import { chooseDesk, saveReviewer } from "@/app/actions";
import { ApiError, currentDesk, desks, me, reviewerName, type Me } from "@/lib/api";
import { when } from "@/lib/format";
import { AutoRefresh, DeskSelect, Flash, Submit } from "./client";

type Message = { done?: string; error?: string };

export async function Shell({ children, active, message }: {
  children: React.ReactNode; active: "desk" | "holds"; message?: Message;
}) {
  const desk = await currentDesk();
  const reviewer = await reviewerName();
  let clock: Me | null = null;
  let down: string | null = null;
  try {
    clock = await me();
  } catch (e) {
    down = e instanceof ApiError ? e.message : "The TrailGuard API could not be reached.";
  }
  return (
    <>
      <AutoRefresh seconds={15} />
      <header className="top">
        <Link href="/" className="brand">TrailGuard</Link>
        <nav aria-label="Main">
          <Link href="/" aria-current={active === "desk" ? "page" : undefined}>Desk</Link>
          <Link href="/holds" aria-current={active === "holds" ? "page" : undefined}>Holds</Link>
        </nav>
        <form action={chooseDesk} className="desk">
          <label>Fraud desk <DeskSelect desks={Object.keys(desks())} current={desk} /></label>
        </form>
        <form action={saveReviewer} className="who">
          <label>
            Reviewing as
            <input name="reviewer" defaultValue={reviewer} placeholder="Your name" required
                   aria-describedby={reviewer ? undefined : "who-hint"} />
          </label>
          <Submit busy="Saving">{reviewer ? "Change" : "Save"}</Submit>
        </form>
        {clock && (
          <p className="clock">
            <time dateTime={clock.now}>{when(clock.now)}</time> WAT
            {clock.simulated_clock && <span title="Replayed data: the clock is simulated">Replay</span>}
          </p>
        )}
      </header>
      {!reviewer && !down && (
        <p id="who-hint" className="banner">Add your name in the bar above. Confirming or releasing a hold is recorded under it.</p>
      )}
      <main>
        {message?.done && <Flash key={message.done} kind="done" message={message.done} />}
        {message?.error && <Flash key={message.error} kind="error" message={message.error} />}
        {down ? (
          <div className="flash error" role="alert"><p>{down}</p></div>
        ) : children}
      </main>
    </>
  );
}

/** Read the done/error message an action left in the address. */
export function messageFrom(params: Record<string, string | string[] | undefined>): Message {
  const pick = (value: string | string[] | undefined) => (typeof value === "string" ? value : undefined);
  return { done: pick(params.done), error: pick(params.error) };
}
