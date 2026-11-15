"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useFormStatus } from "react-dom";

/** Re-reads the page's data on a timer, so new cases and bank answers appear unprompted. */
export function AutoRefresh({ seconds }: { seconds: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => router.refresh(), seconds * 1000);
    return () => clearInterval(timer);
  }, [router, seconds]);
  return null;
}

export function Submit({ children, busy, className, name, value }: {
  children: React.ReactNode; busy: string; className?: string; name?: string; value?: string;
}) {
  const { pending, data } = useFormStatus();
  const mine = pending && (!name || data?.get(name) === value);
  return (
    <button type="submit" className={className} name={name} value={value} disabled={pending}>
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
