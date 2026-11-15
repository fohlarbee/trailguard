import Link from "next/link";
import { cookies } from "next/headers";
import { chooseDesk, saveReviewer } from "@/app/actions";
import { api, ApiError, currentDesk, desks, type Me } from "@/lib/api";
import { when } from "@/lib/format";
import { AutoRefresh, DeskSelect, Submit } from "./client";

export async function Shell({ children, error }: { children: React.ReactNode; error?: string }) {
  const desk = await currentDesk();
  const reviewer = (await cookies()).get("reviewer")?.value ?? "";
  let me: Me | null = null;
  let down: string | null = null;
  try {
    me = await api<Me>("/v1/me");
  } catch (e) {
    down = e instanceof ApiError ? e.message : "The API could not be reached.";
  }
  return (
    <>
      <AutoRefresh seconds={15} />
      <header className="top">
        <Link href="/" className="brand">TrailGuard</Link>
        <form action={chooseDesk} className="desk">
          <label>Fraud desk <DeskSelect desks={Object.keys(desks())} current={desk} /></label>
        </form>
        <form action={saveReviewer} className="who">
          <label>Reviewing as
            <input name="reviewer" defaultValue={reviewer} placeholder="Your name" required />
          </label>
          <Submit busy="Saving">Save</Submit>
        </form>
        {me && (
          <p className="clock" title={me.simulated_clock ? "Replayed data: the clock is simulated" : undefined}>
            {when(me.now)} WAT{me.simulated_clock && <span> replay</span>}
          </p>
        )}
      </header>
      <main>
        {error && <p className="notice" role="alert">{error}</p>}
        {down ? <p className="notice" role="alert">{down}</p> : children}
      </main>
    </>
  );
}
