"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { Field, IslandButton, Select } from "@/components/vanguard";

const sources = ["greenhouse", "lever", "ashby", "workday", "workable", "smartrecruiters", "keka", "freshteam", "kula", "zohorecruit", "recruitee", "bamboohr", "personio", "teamtailor", "adzuna", "himalayas", "remotive", "remoteok", "arbeitnow", "jsonld"];
const DAYS = [1, 7, 14, 30, 90];

type Props = { value: string; onChange: (v: string) => void; source: string; onSource: (v: string) => void; days: number; onDays: (v: number) => void };

const LABELS: Record<string, string> = { jsonld: "Company sites", remoteok: "RemoteOK", smartrecruiters: "SmartRecruiters", zohorecruit: "Zoho Recruit" };
const sourceLabel = (s: string) => LABELS[s] ?? s[0].toUpperCase() + s.slice(1);

/** Search basis (which resume / persona to match against), source and posting age. */
export function JobSearchBasis(p: Props) {
  const client = useQueryClient();
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
              {sources.map((s) => (
                <option key={s} value={s}>
                  {sourceLabel(s)}
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
      <p className="pl-1 text-xs text-muted-foreground">
        India first: roles in India and remote roles open to India come first, and roles open only outside India are hidden. Search a city outside India to include them.
      </p>
      {(bases.isError || save.isError) && (
        <p role="alert" className="pl-1 text-xs text-danger">
          {getApiErrorMessage(bases.error || save.error, "Search basis unavailable")}
        </p>
      )}
      {bases.data?.options.length === 0 && (
        <p className="pl-1 text-xs text-muted-foreground">Upload a resume to match on skills. Role search still works without one.</p>
      )}
    </div>
  );
}
