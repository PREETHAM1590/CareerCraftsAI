"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { EyeSlash } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { Bezel, PanelTitle, Skeleton, Toggle } from "@/components/vanguard";

export interface LlmPrivacyPrefs {
  llm_hide_personal_info?: boolean;
  llm_hide_name?: boolean;
}

/** What the AI model may see: both switches default on (missing = on). */
export function PrivacyCard() {
  const queryClient = useQueryClient();
  const { data: prefs, isLoading } = useQuery<LlmPrivacyPrefs | null>({
    queryKey: ["preferences"],
    queryFn: async () => (await apiClient.get("/users/me/preferences")).data,
  });
  const [hideInfo, setHideInfo] = useState(true);
  const [hideName, setHideName] = useState(true);
  useEffect(() => {
    setHideInfo(prefs?.llm_hide_personal_info !== false);
    setHideName(prefs?.llm_hide_name !== false);
  }, [prefs]);

  const save = useMutation({
    mutationFn: async (next: LlmPrivacyPrefs) => (await apiClient.patch("/users/me/preferences", next)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["preferences"] });
      toast.success("Privacy setting saved");
    },
    onError: () => {
      setHideInfo(prefs?.llm_hide_personal_info !== false);
      setHideName(prefs?.llm_hide_name !== false);
      toast.error("Save failed");
    },
  });

  const change = (next: LlmPrivacyPrefs) => {
    if (next.llm_hide_personal_info !== undefined) setHideInfo(next.llm_hide_personal_info);
    if (next.llm_hide_name !== undefined) setHideName(next.llm_hide_name);
    save.mutate(next);
  };

  return (
    <Bezel coreClassName="space-y-5 p-6">
      <PanelTitle title="Privacy" icon={<EyeSlash size={16} weight="light" />} />
      {isLoading ? (
        <Skeleton className="h-16 w-full rounded-xl" />
      ) : (
        <>
          <div className="flex items-start gap-4">
            <div className="min-w-0 flex-1 text-sm leading-6 text-muted-foreground">
              <p className="font-medium text-foreground">Hide my personal details from the AI model</p>
              <p className="mt-1 text-xs leading-5">
                Your email, phone number, address, PIN code, profile links and ID numbers (Aadhaar, PAN, passport) are
                swapped for placeholders before anything is sent to your model provider, then put back in the result.
                Application forms are filled from your saved details directly, never by the model.
              </p>
            </div>
            <Toggle
              checked={hideInfo}
              disabled={save.isPending}
              onChange={(next) => change({ llm_hide_personal_info: next })}
              label="Hide my personal details from the AI model"
            />
          </div>
          <div className="flex items-start gap-4">
            <div className="min-w-0 flex-1 text-sm leading-6 text-muted-foreground">
              <p className="font-medium text-foreground">Hide my name too</p>
              <p className="mt-1 text-xs leading-5">
                Your name is replaced the same way and restored in resumes and cover letters. Turn this off if drafts
                read oddly; your contact details stay hidden.
              </p>
            </div>
            <Toggle
              checked={hideInfo && hideName}
              disabled={!hideInfo || save.isPending}
              onChange={(next) => change({ llm_hide_name: next })}
              label="Hide my name from the AI model"
            />
          </div>
        </>
      )}
    </Bezel>
  );
}
