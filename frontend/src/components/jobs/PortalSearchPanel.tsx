"use client";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Buildings, CircleNotch, MagnifyingGlass, X } from "@phosphor-icons/react";
import { toast } from "sonner";
import { getApiErrorMessage } from "@/lib/api";
import {
  deleteSavedBoard,
  listSavedBoards,
  searchCompanyPortal,
  type PortalSearchResult,
  type SavedBoard,
} from "@/lib/job-boards-api";
import { normalizeCareersUrl, splitPlaces } from "@/lib/job-boards";
import { Bezel, Field, IconButton, Input, IslandButton, PanelTitle, Toggle } from "@/components/vanguard";

type Props = {
  /** Called with each successful search so the page can show the jobs in its list. */
  onResults: (result: PortalSearchResult) => void;
};

/** Search one company's careers page directly, and keep a list of saved boards. */
export function PortalSearchPanel({ onResults }: Props) {
  const client = useQueryClient();
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const [places, setPlaces] = useState("");
  const [saveBoard, setSaveBoard] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const boards = useQuery<SavedBoard[]>({ queryKey: ["job-boards"], queryFn: listSavedBoards });

  const search = useMutation({
    mutationFn: (args: { url: string; save: boolean }) =>
      searchCompanyPortal({
        url: args.url,
        query: query.trim(),
        locations: splitPlaces(places),
        save_board: args.save,
      }),
    onMutate: () => setError(null),
    onSuccess: (result) => {
      onResults(result);
      client.invalidateQueries({ queryKey: ["jobs-saved"] });
      if (result.board.saved_board_id) client.invalidateQueries({ queryKey: ["job-boards"] });
      toast.success(
        result.jobs.length
          ? `${result.total_found} matching roles at ${result.board.label}`
          : `No matching roles at ${result.board.label}`,
      );
    },
    onError: (err) => setError(getApiErrorMessage(err, "We could not search that portal. Try again.")),
  });

  const remove = useMutation({
    mutationFn: deleteSavedBoard,
    onSuccess: () => client.invalidateQueries({ queryKey: ["job-boards"] }),
    onError: (err) => toast.error(getApiErrorMessage(err, "Could not remove that board.")),
  });

  function submit(rawUrl: string, save: boolean) {
    const checked = normalizeCareersUrl(rawUrl);
    if (checked.error !== undefined) {
      setError(checked.error);
      return;
    }
    search.mutate({ url: checked.url, save });
  }

  return (
    <Bezel size="md" coreClassName="space-y-5 p-5 md:p-6">
      <div className="space-y-1.5">
        <PanelTitle title="Search a company's careers page" icon={<Buildings size={15} weight="light" />} />
        <p className="pl-[2.625rem] text-xs leading-5 text-muted-foreground">
          Paste a job board link from Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee and similar
          systems. Matching roles are saved to your list.
        </p>
      </div>

      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit(url, saveBoard);
        }}
      >
        <Field label="Careers page link" error={error ?? undefined}>
          {(id) => (
            <Input
              id={id}
              type="url"
              inputMode="url"
              autoComplete="off"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://boards.greenhouse.io/acme"
              aria-invalid={error ? true : undefined}
            />
          )}
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Keywords" hint="Optional">
            {(id) => (
              <Input id={id} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. data engineer" />
            )}
          </Field>
          <Field label="Locations" hint="Optional, comma-separated">
            {(id) => (
              <Input id={id} value={places} onChange={(e) => setPlaces(e.target.value)} placeholder="e.g. Berlin, London" />
            )}
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="inline-flex cursor-pointer items-center gap-2.5 text-[13px] font-medium text-foreground/85">
            <Toggle checked={saveBoard} onChange={setSaveBoard} label="Include in my future searches" />
            Include in my future searches
          </label>
          <IslandButton
            type="submit"
            size="sm"
            disabled={search.isPending}
            icon={
              search.isPending ? (
                <CircleNotch size={14} weight="light" className="animate-spin motion-reduce:animate-none" />
              ) : (
                <MagnifyingGlass size={14} weight="light" />
              )
            }
          >
            {search.isPending ? "Searching…" : "Search this company"}
          </IslandButton>
        </div>
      </form>


      <div className="space-y-2" aria-label="Saved company boards">
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          Saved boards{boards.data?.length ? ` · ${boards.data.length}` : ""}
        </p>
        {boards.isError ? (
          <p className="text-xs text-muted-foreground">Saved boards could not be loaded.</p>
        ) : !boards.data?.length ? (
          <p className="text-xs text-muted-foreground">
            None yet. Boards you save are included every time you search.
          </p>
        ) : (
          <ul className="space-y-2">
            {boards.data.map((board) => (
              <li
                key={board.id}
                className="flex items-center justify-between gap-3 rounded-2xl bg-foreground/[0.025] px-3.5 py-2.5 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.06]"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-foreground">{board.label}</p>
                  <p className="truncate text-xs text-muted-foreground">{board.careers_url}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <IslandButton
                    tone="ghost"
                    size="sm"
                    disabled={search.isPending}
                    onClick={() => {
                      setUrl(board.careers_url);
                      submit(board.careers_url, false);
                    }}
                  >
                    Search
                  </IslandButton>
                  <IconButton
                    aria-label={`Remove ${board.label}`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(board.id)}
                  >
                    <X size={14} weight="light" />
                  </IconButton>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Bezel>
  );
}
