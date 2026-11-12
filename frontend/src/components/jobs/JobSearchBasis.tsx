"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { searchSources } from "@/lib/job-boards";
import { Field, IslandButton, Select, Toggle } from "@/components/vanguard";

const SOURCES = searchSources();
const DAYS = [1, 7, 14, 30, 90];

type Props = { value: string; onChange: (v: string) => void; source: string; onSource: (v: string) => void; days: number; onDays: (v: number) => void };

/** Search basis (which resume / persona to match against), source and posting age. */
export function JobSearchBasis(p: Props) {
  const client = useQueryClient();
  // India focus is a saved preference (missing = on); search reads it server-side.
  const prefs = useQuery<{ india_focus?: boolean } | null>({
    queryKey: ["preferences"],
    queryFn: async () => (await apiClient.get("/users/me/preferences")).data,
  });
  const indiaFocus = useMutation({
    mutationFn: async (next: boolean) => (await apiClient.patch("/users/me/preferences", { india_focus: next })).data,
    onSuccess: () => client.invalidateQueries({ queryKey: ["preferences"] }),
  });
  const indiaOn = indiaFocus.isPending ? indiaFocus.variables !== false : prefs.data?.india_focus !== false;
  const save = useMutation({
    mutationFn: async () => {
      const [kind, id] = p.value.split(":");
      await apiClient.patch("/jobs/search-basis", { basis: { kind, id } });
    },
    onSuccess: () => client.invalidateQueries({ queryKey: ["job-search-bases"] }),
  });
  const bases = useQuery<{ options: { kind: string; id: string; label: string; available: boolean }[] }>({
    queryKey: ["job-search-bases"],
    queryFn: async () => (await apiClient.get("/jobs/search/bases")).data,
  });

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)_minmax(0,0.8fr)]">
        <Field label="Match against">
          {(id) => (
            <div className="flex gap-2">
              <Select id={id} trayClassName="flex-1" value={p.value} onChange={(e) => p.onChange(e.target.value)} disabled={bases.isPending}>
                <option value="">{bases.isPending ? "Loading…" : "Saved default (latest resume)"}</option>
                {bases.data?.options.map((o) => (
                  <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`} disabled={!o.available}>
                    {o.label}
                    {!o.available ? " (unavailable)" : ""}
                  </option>
                ))}
              </Select>
              {p.value && (
                <IslandButton type="button" tone="ghost" size="sm" className="self-center" disabled={save.isPending} onClick={() => save.mutate()}>
                  {save.isPending ? "Saving…" : save.isSuccess ? "Saved" : "Set default"}
                </IslandButton>
              )}
            </div>
          )}
        </Field>
        <Field label="Source">
          {(id) => (
            <Select id={id} value={p.source} onChange={(e) => p.onSource(e.target.value)}>
              <option value="">All public sources</option>
              {SOURCES.map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Posted within">
          {(id) => (
            <Select id={id} value={p.days} onChange={(e) => p.onDays(Number(e.target.value))}>
              {DAYS.map((d) => (
                <option key={d} value={d}>
                  {d === 1 ? "Last 24 hours" : `Last ${d} days`}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </div>
      <div className="flex items-start gap-3 pl-1">
        <Toggle checked={indiaOn} disabled={prefs.isPending || indiaFocus.isPending} onChange={(next) => indiaFocus.mutate(next)} label="India first" />
        <p className="text-xs text-muted-foreground">
          {indiaOn
            ? "India first: roles in India and remote roles open to India come first, and roles open only outside India are hidden from search and never auto-applied to. Search a city outside India to include them."
            : "India first is off: roles anywhere are shown and can be auto-applied to."}
        </p>
      </div>
      {(bases.isError || save.isError || indiaFocus.isError) && (
        <p role="alert" className="pl-1 text-xs text-danger">
          {getApiErrorMessage(bases.error || save.error || indiaFocus.error, "Search basis unavailable")}
        </p>
      )}
      {bases.data?.options.length === 0 && (
        <p className="pl-1 text-xs text-muted-foreground">Upload a resume to match on skills. Role search still works without one.</p>
      )}
    </div>
  );
}
