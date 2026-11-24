"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CaretDown, UserPlus, X } from "@phosphor-icons/react";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { Field, Input, IslandButton } from "@/components/vanguard";

type AccountSite = { origin: string; consent_at: string | null };

/** Sites where the member lets Nova create a job-portal account for them. */
export function AccountSites() {
  const queryClient = useQueryClient();
  const [origin, setOrigin] = useState("");
  const [busy, setBusy] = useState(false);
  const sites = useQuery<AccountSite[]>({
    queryKey: ["account-sites"],
    queryFn: async () => (await apiClient.get("/computer/account-sites")).data,
  });
  const items = sites.data ?? [];

  async function change(run: () => Promise<unknown>, failure: string) {
    setBusy(true);
    try {
      await run();
      await queryClient.invalidateQueries({ queryKey: ["account-sites"] });
    } catch (error) {
      toast.error(getApiErrorMessage(error, failure));
    } finally {
      setBusy(false);
    }
  }

  function allow(event: React.FormEvent) {
    event.preventDefault();
    const value = origin.trim();
    if (!value) return;
    void change(async () => {
      await apiClient.post("/computer/account-sites", { origin: value });
      setOrigin("");
    }, "Could not allow that site. Use its exact address, like https://jobs.example.com");
  }

  return (
    <details className="workspace-disclosure rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium">
        <UserPlus size={18} weight="light" /> Accounts Nova may create
        <span className="ml-auto text-xs font-normal text-muted-foreground">{items.length} {items.length === 1 ? "site" : "sites"}</span>
        <CaretDown className="disclosure-chevron" size={14} weight="light" />
      </summary>
      <p className="my-2 text-xs leading-5 text-muted-foreground">
        When a job needs an account on one of these sites, Nova signs up for you with your application email and a new
        password it saves in your logins, and accepts the site&apos;s standard terms. Nova never sees the password. At most
        3 tries per site a day. Only add sites you&apos;re happy to have an account on.
      </p>
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={allow}>
        <div className="min-w-0 flex-1">
          <Field label="Site address">
            {id => (
              <Input
                id={id}
                value={origin}
                onChange={e => setOrigin(e.target.value)}
                placeholder="https://company.wd5.myworkdayjobs.com"
                inputMode="url"
                autoComplete="off"
              />
            )}
          </Field>
        </div>
        <IslandButton size="sm" tone="ghost" type="submit" disabled={busy || !origin.trim()}>Allow</IslandButton>
      </form>
      <div className="mt-3 space-y-2">
        {sites.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
        {sites.isError && <p role="alert" className="text-xs text-destructive">Could not load your sites.</p>}
        {!sites.isLoading && !sites.isError && !items.length && <p className="text-xs text-muted-foreground">No sites yet. Nova will ask you to sign up yourself.</p>}
        {items.map(site => (
          <div key={site.origin} className="flex items-center justify-between gap-2 text-sm">
            <span className="min-w-0 break-all">
              {site.origin}
              {site.consent_at && <span className="ml-2 text-xs text-muted-foreground">allowed {new Date(site.consent_at).toLocaleDateString()}</span>}
            </span>
            <IslandButton
              size="sm"
              tone="ghost"
              disabled={busy}
              aria-label={`Stop Nova creating accounts on ${site.origin}`}
              onClick={() => void change(
                () => apiClient.delete("/computer/account-sites", { params: { origin: site.origin } }),
                "Could not remove that site.",
              )}
            >
              <X size={14} /> Remove
            </IslandButton>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
        Removing a site stops new sign-ups there. Accounts Nova already created stay in your saved logins until you delete them.
      </p>
    </details>
  );
}
