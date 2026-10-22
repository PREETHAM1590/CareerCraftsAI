"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowsClockwise,
  Browser,
  CaretDown,
  Check,
  Copy,
  DownloadSimple,
  Key,
  Plugs,
  PuzzlePiece,
  ShieldCheck,
} from "@phosphor-icons/react";
import { toast } from "sonner";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  Bezel,
  Eyebrow,
  Hairline,
  IslandButton,
  Notice,
  Skeleton,
  StatusPill,
  bezelCore,
  panelSwap,
  type StatusTone,
} from "@/components/vanguard";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { detectExtension, pairExtension } from "@/lib/extension-bridge";
import { AgentBrowserSetting } from "@/components/agents/AgentBrowserMode";
import { cn } from "@/lib/utils";

type Device = {
  id: string;
  name: string;
  created_at: string | null;
  last_seen_at: string | null;
};

type Pairing = { device_id: string; name: string; token: string };

function browserName(): string {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? "Edge" : /Brave/.test(ua) ? "Brave" : /Chrome\//.test(ua) ? "Chrome" : "Browser";
  const os = /Mac OS X/.test(ua) ? "macOS" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser} on ${os}` : browser;
}

function lastSeen(value: string | null): string {
  if (!value) return "Never connected";
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60_000);
  if (minutes < 2) return "Active now";
  if (minutes < 60) return `Seen ${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `Seen ${hours} h ago`;
  return `Seen ${new Date(value).toLocaleDateString()}`;
}

/** Numbered install guide shared by the inline disclosure and the post-download dialog. */
function SetupSteps() {
  const steps: ReactNode[] = [
    <>Unzip the downloaded file. You get a folder named <code>careercraft-extension</code>.</>,
    <>
      Open <code>chrome://extensions</code> (Edge: <code>edge://extensions</code>, Brave:{" "}
      <code>brave://extensions</code>) in a new tab and turn on <strong>Developer mode</strong>.
    </>,
    <>
      Click <strong>Load unpacked</strong> and select the <code>careercraft-extension</code> folder (the one that
      contains <code>manifest.json</code>).
    </>,
    <>Pin <strong>CareerCraft AI — Apply Assistant</strong> from the puzzle icon in the toolbar.</>,
    <>
      Reload this CareerCraft page, then click <strong>Connect this browser</strong>. The site pairs with the
      extension automatically.
    </>,
  ];
  return (
    <div
      className={cn(
        "space-y-4 text-sm leading-6 text-muted-foreground",
        "[&_strong]:font-medium [&_strong]:text-foreground",
        "[&_code]:rounded-md [&_code]:bg-foreground/[0.05] [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:font-geist-mono [&_code]:text-[12px] [&_code]:text-foreground [&_code]:ring-1 [&_code]:ring-foreground/[0.06] dark:[&_code]:bg-white/[0.06] dark:[&_code]:ring-white/10",
      )}
    >
      <ol className="space-y-3">
        {steps.map((step, index) => (
          <li key={index} className="grid grid-cols-[2rem_1fr] items-start gap-3">
            <span
              aria-hidden
              className="grid h-7 w-7 place-items-center rounded-full bg-card font-geist-mono text-[11px] tabular-nums text-foreground ring-1 ring-foreground/[0.08] shadow-bezel-core dark:bg-white/[0.05] dark:ring-white/10 dark:shadow-bezel-core-dark"
            >
              {String(index + 1).padStart(2, "0")}
            </span>
            <span className="min-w-0 pt-0.5 text-pretty">
              <span className="sr-only">Step {index + 1}: </span>
              {step}
            </span>
          </li>
        ))}
      </ol>
      <p className="pl-11 text-xs leading-5">
        Chrome 110 or newer. To update later, download again, replace the folder and press the reload icon on the
        extension in <code>chrome://extensions</code>.
      </p>
    </div>
  );
}

export function BrowserExtensionCard() {
  const queryClient = useQueryClient();
  const [installedVersion, setInstalledVersion] = useState<string | null | undefined>(undefined);
  const [code, setCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);

  useEffect(() => {
    detectExtension().then(setInstalledVersion);
  }, []);

  const devices = useQuery<{ devices: Device[] }>({
    queryKey: ["extension-devices"],
    queryFn: async () => (await apiClient.get("/extension/devices")).data,
    refetchInterval: 30_000,
  });

  const pair = useMutation({
    mutationFn: async () => (await apiClient.post<Pairing>("/extension/pair", { name: browserName() })).data,
    onSuccess: async (pairing) => {
      const connected = installedVersion ? await pairExtension(pairing.token) : false;
      if (connected) {
        setCode(null);
        toast.success("This browser is connected. Applications will open here for your review.");
      } else {
        // Shown once: the server keeps only a hash of it.
        setCode(pairing.token);
      }
      queryClient.invalidateQueries({ queryKey: ["extension-devices"] });
      queryClient.invalidateQueries({ queryKey: ["computer-mode"] });
    },
    onError: (error) => toast.error(getApiErrorMessage(error, "Could not create a connection code")),
  });

  const revoke = useMutation({
    mutationFn: async (id: string) => apiClient.delete(`/extension/devices/${id}`),
    onSuccess: () => {
      toast.success("Browser disconnected");
      queryClient.invalidateQueries({ queryKey: ["extension-devices"] });
      queryClient.invalidateQueries({ queryKey: ["computer-mode"] });
    },
    onError: (error) => toast.error(getApiErrorMessage(error, "Could not disconnect that browser")),
  });

  const download = useMutation({
    mutationFn: async () => apiClient.get("/extension/download", { responseType: "blob" }),
    onSuccess: (response) => {
      const url = window.URL.createObjectURL(new Blob([response.data], { type: "application/zip" }));
      const match = (response.headers["content-disposition"] as string | undefined)?.match(/filename="?([^"]+)"?/);
      const a = document.createElement("a");
      a.href = url;
      a.download = match?.[1] ?? "careercraft-extension.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      setSetupOpen(true);
    },
    onError: (error) => toast.error(getApiErrorMessage(error, "Could not download the extension")),
  });

  async function copyCode() {
    if (!code) return;
    await navigator.clipboard.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  const list = devices.data?.devices ?? [];

  const detection: { tone: StatusTone; label: string; live: boolean } =
    installedVersion === undefined
      ? { tone: "neutral", label: "Checking", live: true }
      : installedVersion
        ? { tone: "success", label: `Installed · v${installedVersion}`, live: false }
        : { tone: "warning", label: "Not detected", live: false };

  return (
    <Bezel
      size="lg"
      tone="primary"
      lifted
      className="h-full"
      coreClassName="flex flex-col gap-8 p-6 sm:p-8 md:p-10"
    >
      {/* ── Masthead ─────────────────────────────────────────────── */}
      <div className="flex flex-col gap-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Eyebrow tone="primary">
            <PuzzlePiece size={12} weight="light" aria-hidden />
            Applies in your browser
          </Eyebrow>
          <StatusPill tone={detection.tone} live={detection.live}>
            {detection.label}
          </StatusPill>
        </div>

        <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
          <div className="min-w-0">
            <h2 className="font-geist text-3xl font-semibold leading-[1.02] tracking-[-0.04em] text-foreground md:text-[2.6rem]">
              Browser extension
            </h2>
            <p className="mt-4 max-w-[54ch] text-pretty text-[15px] leading-7 text-muted-foreground">
              Apply from your own browser, where you are already signed in to LinkedIn and Naukri. The extension
              fills each application and waits — nothing is submitted until you press Submit in its review panel.
            </p>
            <p className="mt-3 text-xs leading-5 text-muted-foreground" aria-live="polite">
              {installedVersion === undefined
                ? "Checking this browser…"
                : installedVersion
                  ? `Extension installed in this browser (v${installedVersion}).`
                  : "Extension not detected in this browser. Download it and follow the setup steps below."}
            </p>
          </div>
          <span
            aria-hidden
            className="hidden h-20 w-20 place-items-center rounded-[1.75rem] bg-primary/10 text-primary ring-1 ring-primary/20 shadow-bezel-core dark:shadow-bezel-core-dark md:grid"
          >
            <PuzzlePiece size={34} weight="light" />
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <IslandButton
            tone="primary"
            size="md"
            trailing={
              installedVersion ? (
                <Browser size={15} weight="light" />
              ) : (
                <ArrowsClockwise size={15} weight="light" />
              )
            }
            disabled={pair.isPending || installedVersion === undefined}
            onClick={() => installedVersion ? pair.mutate() : window.location.reload()}
          >
            {pair.isPending ? "Connecting…" : installedVersion ? "Connect this browser" : "Reload to connect"}
          </IslandButton>
          <IslandButton
            tone="ghost"
            size="md"
            icon={<DownloadSimple size={16} weight="light" />}
            disabled={download.isPending}
            onClick={() => download.mutate()}
          >
            {download.isPending ? "Preparing…" : "Download extension"}
          </IslandButton>
        </div>

        {installedVersion === null ? (
          // The bridge only runs by itself on the hosted site and localhost. On
          // any other deployment the extension can't be detected until it has
          // been paired once, so a connection code is the only way in.
          <p className="max-w-[60ch] text-xs leading-5 text-muted-foreground">
            Using CareerCraft on a different address, or already installed and still not detected?{" "}
            <button
              type="button"
              className="rounded-sm font-medium text-primary underline decoration-primary/40 underline-offset-4 transition-colors duration-500 ease-vanguard hover:decoration-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
              disabled={pair.isPending}
              onClick={() => pair.mutate()}
            >
              Get a connection code
            </button>{" "}
            and paste it in the extension popup.
          </p>
        ) : null}
      </div>

      {/* ── One-time connection code ─────────────────────────────── */}
      <AnimatePresence initial={false}>
        {code ? (
          <motion.div key="pairing-code" variants={panelSwap} initial="hidden" animate="show" exit="exit">
            <Bezel size="md" tone="default" coreClassName="p-5 md:p-6">
              <div className="flex items-start gap-3">
                <span
                  aria-hidden
                  className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-warning/10 text-warning ring-1 ring-warning/25"
                >
                  <Key size={16} weight="light" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-foreground">Paste this code in the extension popup</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    Open the extension popup, enter {typeof window !== "undefined" ? window.location.origin : "this site's address"}{" "}
                    as the CareerCraft URL, and paste this code. It is shown only once.
                  </p>
                </div>
              </div>
              <div className="mt-4 flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1 rounded-2xl bg-foreground/[0.03] p-1 ring-1 ring-foreground/[0.07] dark:bg-white/[0.03] dark:ring-white/10">
                  <code className="block min-w-0 truncate rounded-xl bg-card px-4 py-3 font-geist-mono text-xs text-foreground shadow-bezel-core dark:bg-background/70 dark:shadow-bezel-core-dark">
                    {code}
                  </code>
                </div>
                <IslandButton
                  tone="ghost"
                  size="sm"
                  className="shrink-0 self-start sm:self-auto"
                  icon={copied ? <Check size={14} weight="light" /> : <Copy size={14} weight="light" />}
                  onClick={copyCode}
                >
                  {copied ? "Copied" : "Copy"}
                </IslandButton>
              </div>
              <span className="sr-only" aria-live="polite">{copied ? "Code copied to clipboard" : ""}</span>
            </Bezel>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <Hairline />

      {/* ── Connected browsers + install guide ───────────────────── */}
      <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <div className="min-w-0">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
              Connected browsers
            </h3>
            {!devices.isLoading && !devices.isError ? (
              <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground">
                {String(list.length).padStart(2, "0")}
              </span>
            ) : null}
          </div>

          {devices.isLoading ? (
            <div className="mt-4 space-y-2" aria-hidden>
              <Skeleton className="h-16 rounded-2xl" />
              <Skeleton className="h-16 rounded-2xl" />
            </div>
          ) : devices.isError ? (
            <Notice tone="danger" className="mt-4">
              <p>Could not load connected browsers.</p>
            </Notice>
          ) : list.length === 0 ? (
            <div className="mt-4 flex items-center gap-3 rounded-2xl bg-foreground/[0.02] px-4 py-5 ring-1 ring-foreground/[0.07] dark:bg-white/[0.02] dark:ring-white/10">
              <span
                aria-hidden
                className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-foreground/[0.04] text-muted-foreground ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10"
              >
                <Browser size={16} weight="light" />
              </span>
              <p className="text-sm text-muted-foreground">No browser connected yet.</p>
            </div>
          ) : (
            <ul className="mt-4 divide-y divide-foreground/[0.06] overflow-hidden rounded-2xl bg-card ring-1 ring-foreground/[0.07] shadow-bezel-core dark:divide-white/[0.07] dark:bg-background/40 dark:ring-white/10 dark:shadow-bezel-core-dark">
              {list.map((device) => {
                const seen = lastSeen(device.last_seen_at);
                const active = seen === "Active now";
                return (
                  <li key={device.id} className="flex items-center justify-between gap-3 px-4 py-3.5">
                    <div className="flex min-w-0 items-center gap-3">
                      <span
                        aria-hidden
                        className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-foreground/[0.04] text-foreground/80 ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10"
                      >
                        <Browser size={16} weight="light" />
                      </span>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium tracking-[-0.01em] text-foreground">{device.name}</p>
                        <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                          <span
                            aria-hidden
                            className={cn("h-1.5 w-1.5 rounded-full", active ? "bg-success" : "bg-muted-foreground/50")}
                          />
                          {seen}
                        </p>
                      </div>
                    </div>
                    <IslandButton
                      tone="quiet"
                      size="sm"
                      className="shrink-0"
                      icon={<Plugs size={14} weight="light" />}
                      aria-label={`Disconnect ${device.name}`}
                      disabled={revoke.isPending}
                      onClick={() => {
                        if (window.confirm(`Disconnect ${device.name}? It will stop receiving applications.`)) {
                          revoke.mutate(device.id);
                        }
                      }}
                    >
                      Disconnect
                    </IslandButton>
                  </li>
                );
              })}
            </ul>
          )}

          <p className="mt-4 flex items-start gap-2 text-xs leading-5 text-muted-foreground">
            <ShieldCheck size={14} weight="light" aria-hidden className="mt-0.5 shrink-0 text-primary" />
            Applications open only in browsers listed here. Disconnecting a browser stops it from receiving them.
          </p>
        </div>

        {installedVersion !== undefined ? (
          <details
            className="group min-w-0 self-start rounded-2xl bg-foreground/[0.025] p-1 ring-1 ring-foreground/[0.06] dark:bg-white/[0.025] dark:ring-white/10"
            open={!installedVersion}
          >
            <summary
              className={cn(
                "flex cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-4 py-3 text-sm font-medium text-foreground",
                "transition-colors duration-500 ease-vanguard hover:bg-foreground/[0.03] dark:hover:bg-white/[0.04]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden",
              )}
            >
              How to install and connect the extension
              <span
                aria-hidden
                className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-foreground/[0.05] transition-transform duration-500 ease-vanguard group-open:rotate-180 dark:bg-white/10"
              >
                <CaretDown size={13} weight="light" />
              </span>
            </summary>
            <div className="px-4 pb-4 pt-2">
              <SetupSteps />
            </div>
          </details>
        ) : (
          <Skeleton className="h-14 rounded-2xl" />
        )}
      </div>

      <Hairline />

      <AgentBrowserSetting />

      <Dialog open={setupOpen} onOpenChange={setSetupOpen}>
        <DialogContent
          className={cn(
            "max-h-[90vh] max-w-xl gap-0 overflow-y-auto rounded-[2rem] border-0 bg-background p-1.5 font-geist ring-1 ring-foreground/[0.08] dark:ring-white/10 sm:rounded-[2rem]",
            "shadow-[0_40px_80px_-48px_hsl(var(--foreground)/0.18),0_12px_24px_-20px_hsl(var(--foreground)/0.08)]",
          )}
        >
          <div className={cn(bezelCore("lg"), "grid gap-7 p-6 sm:p-8")}>
            <DialogHeader className="space-y-3 text-left">
              <Eyebrow tone="primary" className="w-fit">
                <DownloadSimple size={12} weight="light" aria-hidden />
                Download started
              </Eyebrow>
              <DialogTitle className="font-geist text-2xl font-semibold leading-tight tracking-[-0.03em]">
                Set up the CareerCraft extension
              </DialogTitle>
              <DialogDescription className="text-sm leading-6">
                Your download has started. Install it in this browser in a few steps.
              </DialogDescription>
            </DialogHeader>
            <SetupSteps />
            <div className="flex justify-end">
              <IslandButton tone="primary" size="sm" trailing={<Check size={14} weight="light" />} onClick={() => setSetupOpen(false)}>
                Done
              </IslandButton>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </Bezel>
  );
}
