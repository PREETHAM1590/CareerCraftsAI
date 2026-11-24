// Assisted apply in the member's OWN browser. Nothing is submitted for them:
// the extension (or a plain tab) opens the employer's page and the member
// clicks Submit. No third-party credentials are stored by us.
import { toast } from "sonner";
import { apiClient } from "@/lib/api";
import { setApplyState } from "@/lib/applications-api";
import { detectExtension, wakeExtension } from "@/lib/extension-bridge";
import { safeHttpUrl } from "@/lib/safe-url";

/**
 * Call synchronously from a click handler: window.open must run before any
 * await or popup blockers drop it. (No noopener: we keep the handle to
 * navigate or close the placeholder tab.)
 */
export async function startAssistedApply(app: { id: string; job_url: string | null }): Promise<boolean> {
  const jobUrl = safeHttpUrl(app.job_url);
  const tab = window.open("about:blank", "_blank");
  if (tab) tab.opener = null; // we keep the handle; the opened page can't script ours
  try {
    const extension = await detectExtension();
    if (extension) {
      await apiClient.post(`/jobs/applications/${app.id}/prepare-apply`, { live_browser: false });
      wakeExtension(); // the extension opens + prefills the job itself
      tab?.close();
    } else if (tab && jobUrl) {
      tab.location.href = jobUrl;
    } else {
      tab?.close();
      if (!jobUrl) throw new Error("no job url");
      toast.message("Your browser blocked the new tab.", {
        action: { label: "Open job", onClick: () => window.open(jobUrl, "_blank", "noopener,noreferrer") },
      });
    }
    await setApplyState(app.id, "opened");
    return true;
  } catch {
    tab?.close();
    toast.error("Could not start the application. Open the job page directly instead.");
    return false;
  }
}
