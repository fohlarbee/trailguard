"use server";

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";

const YEAR = 60 * 60 * 24 * 365;

export async function chooseDesk(formData: FormData) {
  (await cookies()).set("desk", String(formData.get("desk")), { maxAge: YEAR });
  revalidatePath("/", "layout");
}

export async function saveReviewer(formData: FormData) {
  (await cookies()).set("reviewer", String(formData.get("reviewer")).trim(), { maxAge: YEAR });
  revalidatePath("/", "layout");
}

function back(formData: FormData, error?: string): never {
  const from = String(formData.get("from") || "/");
  redirect(error ? `${from}?error=${encodeURIComponent(error)}` : from);
}

export async function reviewHold(formData: FormData) {
  const reviewer = (await cookies()).get("reviewer")?.value;
  if (!reviewer) back(formData, "Enter your name at the top of the page before deciding on a hold.");
  try {
    await api(`/v1/holds/${formData.get("hold_id")}/review`, {
      method: "POST", body: { decision: formData.get("decision"), reviewer },
    });
  } catch (error) {
    back(formData, error instanceof ApiError ? error.message : "The decision could not be saved.");
  }
  revalidatePath("/", "layout");
  back(formData);
}

export async function reduceHold(formData: FormData) {
  const reviewer = (await cookies()).get("reviewer")?.value;
  if (!reviewer) back(formData, "Enter your name at the top of the page before changing a hold.");
  try {
    await api(`/v1/holds/${formData.get("hold_id")}/reduce`, { method: "POST", body: { reviewer } });
  } catch (error) {
    back(formData, error instanceof ApiError ? error.message : "The hold could not be reduced.");
  }
  revalidatePath("/", "layout");
  back(formData);
}

export async function runInvestigator(formData: FormData) {
  try {
    await api(`/v1/cases/${formData.get("case_id")}/investigate`, { method: "POST" });
  } catch (error) {
    back(formData, error instanceof ApiError ? error.message : "The investigator could not run.");
  }
  revalidatePath("/", "layout");
  back(formData);
}
