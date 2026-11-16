"use server";

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { naira } from "@/lib/format";

const YEAR = 60 * 60 * 24 * 365;

export async function chooseDesk(formData: FormData) {
  (await cookies()).set("desk", String(formData.get("desk")), { maxAge: YEAR });
  revalidatePath("/", "layout");
}

export async function saveReviewer(formData: FormData) {
  (await cookies()).set("reviewer", String(formData.get("reviewer")).trim(), { maxAge: YEAR });
  revalidatePath("/", "layout");
}

/** Go back to the page the form was on, with a message for the person who acted. */
function back(formData: FormData, message: { done?: string; error?: string }): never {
  const target = new URL(String(formData.get("from") || "/"), "http://local");
  target.searchParams.delete("done");
  target.searchParams.delete("error");
  for (const [key, value] of Object.entries(message)) if (value) target.searchParams.set(key, value);
  redirect(target.pathname + target.search + target.hash);
}

function failure(error: unknown, fallback: string) {
  return { error: error instanceof ApiError ? error.message : fallback };
}

export async function reviewHold(formData: FormData) {
  const reviewer = (await cookies()).get("reviewer")?.value;
  if (!reviewer) back(formData, { error: "Add your name at the top of the page before deciding on a hold." });
  const decision = String(formData.get("decision"));
  const label = String(formData.get("label") ?? "");
  try {
    await api(`/v1/holds/${formData.get("hold_id")}/review`, { method: "POST", body: { decision, reviewer } });
  } catch (error) {
    back(formData, failure(error, "The decision could not be saved."));
  }
  revalidatePath("/", "layout");
  back(formData, {
    done: decision === "confirm"
      ? `Hold on ${label} confirmed. It will not lapse.`
      : `Hold on ${label} released.`,
  });
}

export async function reduceHold(formData: FormData) {
  const reviewer = (await cookies()).get("reviewer")?.value;
  if (!reviewer) back(formData, { error: "Add your name at the top of the page before changing a hold." });
  let held: number | null = null;
  try {
    const hold = await api<{ held_kobo: number }>(`/v1/holds/${formData.get("hold_id")}/reduce`,
                                                   { method: "POST", body: { reviewer } });
    held = hold.held_kobo;
  } catch (error) {
    back(formData, failure(error, "The hold could not be reduced."));
  }
  revalidatePath("/", "layout");
  back(formData, { done: `Hold on ${formData.get("label")} reduced to ${naira(held)}.` });
}

const OUTCOME: Record<string, string> = {
  completed: "The investigator finished. Its handover is below.",
  refused: "The model declined to work this case. See the note below.",
  stopped: "The investigator stopped before finishing. See the note below.",
  failed: "The investigator could not run. See the note below.",
};

export async function runInvestigator(formData: FormData) {
  let outcome = "";
  try {
    outcome = (await api<{ outcome: string }>(`/v1/cases/${formData.get("case_id")}/investigate`,
                                              { method: "POST" })).outcome;
  } catch (error) {
    back(formData, failure(error, "The investigator could not run."));
  }
  revalidatePath("/", "layout");
  const message = OUTCOME[outcome] ?? "The investigator finished.";
  back(formData, outcome === "completed" ? { done: message } : { error: message });
}
