"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useUser } from "@clerk/nextjs";
import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowsClockwise,
  CalendarBlank,
  CalendarDots,
  CloudSlash,
  EnvelopeSimple,
  PaperPlaneTilt,
  GoogleDriveLogo,
  GoogleLogo,
  Info,
  MicrosoftOutlookLogo,
  Plugs,
  WarningCircle,
  WindowsLogo,
} from "@phosphor-icons/react";
import { toast } from "sonner";

import { BrowserExtensionCard } from "@/components/settings/BrowserExtensionCard";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  Bezel,
  EmptyPanel,
  Hairline,
  IslandButton,
  IslandLink,
  Notice,
  PageHero,
  Reveal,
  Screen,
  Section,
  Stat,
  StatusPill,
  type StatusTone,
} from "@/components/vanguard";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { openNangoConnectWindow } from "@/lib/nango-connect";
import { GitHubSettings } from "@/components/integrations/GitHubSettings";
import { cn } from "@/lib/utils";

type Provider = "gmail" | "gmail_send" | "google_drive" | "google_calendar" | "outlook_mail" | "outlook_calendar";
type ConnectionStatus = "pending" | "connected" | "disconnected" | "error" | "revoked";

type Connection = {
  provider: Provider;
  status: ConnectionStatus;
  connected_at: string | null;
  last_synced_at: string | null;
  account_email: string | null;
};

type ConnectSession = {
  provider: Provider;
  connect_link: string;
  expires_at: string | null;
};

type ProviderDef = { id: Provider; name: string; description: string };

const PROVIDERS: Array<ProviderDef> = [
  { id: "gmail", name: "Gmail", description: "Read context and send approved outreach." },
  { id: "gmail_send", name: "Gmail (send only)", description: "Send approved outreach. CareerCraft can't read your inbox." },
  { id: "google_drive", name: "Google Drive", description: "Save documents you explicitly export." },
  { id: "google_calendar", name: "Google Calendar", description: "Schedule approved interview events." },
  { id: "outlook_mail", name: "Microsoft Outlook Mail", description: "Read context and send approved outreach." },
  { id: "outlook_calendar", name: "Microsoft Outlook Calendar", description: "Schedule approved interview events." },
];

const PROVIDER_ICON: Record<Provider, ReactNode> = {
  gmail: <EnvelopeSimple size={18} weight="light" />,
  gmail_send: <PaperPlaneTilt size={18} weight="light" />,
  google_drive: <GoogleDriveLogo size={18} weight="light" />,
  google_calendar: <CalendarBlank size={18} weight="light" />,
  outlook_mail: <MicrosoftOutlookLogo size={18} weight="light" />,
  outlook_calendar: <CalendarDots size={18} weight="light" />,
};

const GOOGLE_IDS: Provider[] = ["gmail", "gmail_send", "google_drive", "google_calendar"];
const MICROSOFT_IDS: Provider[] = ["outlook_mail", "outlook_calendar"];

function providerDef(id: Provider): ProviderDef {
  return PROVIDERS.find((provider) => provider.id === id) as ProviderDef;
}

function statusLabel(status: ConnectionStatus | undefined): string {
  return status ? status.replace("-", " ") : "disconnected";
}

function statusTone(status: ConnectionStatus | undefined): StatusTone {
  switch (status) {
    case "connected":
      return "success";
    case "pending":
      return "warning";
    case "error":
    case "revoked":
      return "danger";
    default:
      return "neutral";
  }
}

function formatDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

type TileVariant = "feature" | "compact" | "row";

interface ProviderTileProps {
  provider: ProviderDef;
  connection: Connection | undefined;
  variant: TileVariant;
  loading: boolean;
  busy: boolean;
  awaitingPopup: boolean;
  accountMismatch: boolean;
  loginEmail: string | null;
  onConnect: () => void;
  onDisconnect: () => void;
  children?: ReactNode;
}

/** One provider surface. Variants vary the composition inside the bento; behaviour is identical. */
function ProviderTile({
  provider,
  connection,
  variant,
  loading,
  busy,
  awaitingPopup,
  accountMismatch,
  loginEmail,
  onConnect,
  onDisconnect,
  children,
}: ProviderTileProps) {
  const connected = connection?.status === "connected";
  const pending = connection?.status === "pending";
  const since = connected ? formatDate(connection?.connected_at ?? null) : null;

  const status = (
    <span aria-live="polite">
      <StatusPill tone={loading ? "neutral" : statusTone(connection?.status)} live={loading || pending || awaitingPopup}>
        <span className="sr-only">Status: </span>
        <span className="capitalize">{loading ? "checking" : statusLabel(connection?.status)}</span>
      </StatusPill>
    </span>
  );

  const action = connected ? (
    <IslandButton
      tone="ghost"
      size="sm"
      icon={<Plugs size={14} weight="light" />}
      aria-label={`Disconnect ${provider.name}`}
      disabled={busy}
      onClick={onDisconnect}
    >
      Disconnect
    </IslandButton>
  ) : (
    <IslandButton
      tone={pending ? "ghost" : "primary"}
      size="sm"
      icon={pending ? <ArrowsClockwise size={14} weight="light" /> : undefined}
      trailing={pending ? undefined : true}
      aria-label={`${pending ? "Reconnect" : "Connect"} ${provider.name}`}
      disabled={busy}
      onClick={onConnect}
    >
      {pending ? "Reconnect" : "Connect"}
    </IslandButton>
  );

  const medallion = (
    <span
      aria-hidden
      className={cn(
        "grid shrink-0 place-items-center text-foreground/80 ring-1 shadow-bezel-core dark:shadow-bezel-core-dark",
        "transition-colors duration-500 ease-vanguard",
        connected
          ? "bg-success/10 text-success ring-success/20"
          : "bg-card ring-foreground/[0.07] dark:bg-white/[0.05] dark:ring-white/10",
        variant === "feature" ? "h-12 w-12 rounded-2xl" : "h-10 w-10 rounded-xl",
      )}
    >
      {PROVIDER_ICON[provider.id]}
    </span>
  );

  const accountLine = connection?.account_email ? (
    <p className="min-w-0 truncate text-xs text-muted-foreground" title={connection.account_email}>
      Connected account: <span className="font-geist-mono text-[11px] text-foreground">{connection.account_email}</span>
    </p>
  ) : null;

  const mismatchLine = accountMismatch ? (
    <p role="alert" className="flex items-start gap-1.5 text-xs leading-5 text-warning">
      <WarningCircle size={14} weight="light" aria-hidden className="mt-0.5 shrink-0" />
      <span>
        This Google account does not match your sign-in email ({loginEmail}). It has been disconnected; connect the
        matching account.
      </span>
    </p>
  ) : null;

  const popupLine = awaitingPopup ? (
    <p className="text-xs text-primary" aria-live="polite">
      Finish signing in in the popup window…
    </p>
  ) : null;

  const surface = cn(
    "group/tile relative rounded-[1.25rem] ring-1 transition-colors duration-500 ease-vanguard",
    connected
      ? "bg-success/[0.025] ring-success/20 hover:bg-success/[0.045]"
      : "bg-foreground/[0.02] ring-foreground/[0.06] hover:bg-foreground/[0.035] dark:bg-white/[0.02] dark:ring-white/[0.08] dark:hover:bg-white/[0.035]",
  );

  if (variant === "feature") {
    return (
      <article className={cn(surface, "flex flex-col gap-6 p-5 md:p-6")}>
        <div className="flex items-start justify-between gap-4">
          {medallion}
          {status}
        </div>
        <div className="min-w-0">
          <h3 className="font-geist text-2xl font-semibold tracking-[-0.035em] text-foreground">{provider.name}</h3>
          <p className="mt-1.5 max-w-[42ch] text-sm leading-6 text-muted-foreground">{provider.description}</p>
        </div>
        {accountLine || mismatchLine || popupLine || children ? (
          <div className="space-y-2.5">
            {accountLine}
            {mismatchLine}
            {popupLine}
            {children}
          </div>
        ) : null}
        <div className="mt-auto flex flex-wrap items-center justify-between gap-3">
          {since ? (
            <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground">since {since}</span>
          ) : (
            <span aria-hidden />
          )}
          {action}
        </div>
      </article>
    );
  }

  if (variant === "row") {
    return (
      <article className={cn(surface, "flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between md:p-5")}>
        <div className="flex min-w-0 items-start gap-3.5">
          {medallion}
          <div className="min-w-0 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground">{provider.name}</h3>
              {status}
            </div>
            <p className="text-sm leading-6 text-muted-foreground">{provider.description}</p>
            {accountLine}
            {popupLine}
          </div>
        </div>
        <div className="shrink-0 self-end sm:self-center">{action}</div>
      </article>
    );
  }

  return (
    <article className={cn(surface, "flex min-h-52 flex-col gap-4 p-4 md:p-5")}>
      <div className="flex items-start justify-between gap-3">
        {medallion}
        {status}
      </div>
      <div className="min-w-0 space-y-1.5">
        <h3 className="font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground">{provider.name}</h3>
        <p className="text-[13px] leading-5 text-muted-foreground">{provider.description}</p>
        {accountLine}
        {popupLine}
      </div>
      <div className="mt-auto">{action}</div>
    </article>
  );
}

/** Bezel grouping a vendor's services under a single masthead. */
function ProviderGroup({
  eyebrow,
  title,
  icon,
  connected,
  total,
  children,
}: {
  eyebrow: string;
  title: string;
  icon: ReactNode;
  connected: number;
  total: number;
  children: ReactNode;
}) {
  return (
    <Bezel size="lg" className="h-full" coreClassName="flex h-full flex-col gap-5 p-4 sm:p-5 md:p-6">
      <div className="flex items-start justify-between gap-4 px-1 pt-1">
        <div className="flex min-w-0 items-center gap-3">
          <span
            aria-hidden
            className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-foreground/[0.04] text-foreground/80 ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10"
          >
            {icon}
          </span>
          <div className="min-w-0">
            <p className="text-[10px] font-medium uppercase tracking-[0.2em] text-muted-foreground">{eyebrow}</p>
            <h2 className="mt-0.5 font-geist text-xl font-semibold tracking-[-0.03em] text-foreground">{title}</h2>
          </div>
        </div>
        <p className="shrink-0 pt-1 font-geist-mono text-xs tabular-nums text-muted-foreground">
          <span className="text-foreground">{connected}</span>/{total}
          <span className="sr-only"> connected</span>
        </p>
      </div>
      <Hairline />
      {children}
    </Bezel>
  );
}

export default function IntegrationsSettingsPage() {
  const queryClient = useQueryClient();
  const { user: authUser } = useUser();
  const [connectingProvider, setConnectingProvider] = useState<Provider | null>(null);
  const [connectWindow, setConnectWindow] = useState<Window | null>(null);
  const connections = useQuery<Connection[]>({
    queryKey: ["integrations"],
    queryFn: async () => (await apiClient.get<Connection[]>("/integrations")).data,
    refetchInterval: connectingProvider ? 2_000 : false,
    // 503 means integrations are switched off on this server; retrying won't change that.
    retry: (failureCount, error) => !(axios.isAxiosError(error) && error.response?.status === 503) && failureCount < 2,
  });
  const loginEmail = authUser?.primaryEmailAddress?.emailAddress ?? null;
  const enforceGmailMatch = loginEmail?.toLowerCase().endsWith("@gmail.com") ?? false;

  const connect = useMutation({
    mutationFn: async ({ provider }: { provider: Provider; popup: Window }) => (
      await apiClient.post<ConnectSession>("/integrations/connect-session", {
        provider,
        return_path: "/settings/integrations",
      })
    ).data,
    onSuccess: (session, { provider, popup }) => {
      popup.location.href = session.connect_link;
      setConnectingProvider(provider);
      setConnectWindow(popup);
    },
    onError: () => toast.error("Could not start the connection."),
  });

  const disconnect = useMutation({
    mutationFn: async (provider: Provider) => apiClient.delete(`/integrations/${provider}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["integrations"] });
      toast.success("Integration disconnected");
    },
    onError: () => toast.error("Could not disconnect the integration."),
  });

  const byProvider = useMemo(
    () => new Map(connections.data?.map((connection) => [connection.provider, connection])),
    [connections.data],
  );
  const connectedCount = [...byProvider.values()].filter((connection) => connection.status === "connected").length;

  useEffect(() => {
    if (!connectingProvider) return;
    if (byProvider.get(connectingProvider)?.status === "connected") {
      connectWindow?.close();
      setConnectingProvider(null);
      setConnectWindow(null);
      toast.success("Integration connected");
      return;
    }
    const pendingConnection = byProvider.get(connectingProvider);
    if (
      pendingConnection?.status === "revoked" &&
      enforceGmailMatch &&
      pendingConnection.account_email &&
      loginEmail &&
      pendingConnection.account_email.toLowerCase() !== loginEmail.toLowerCase()
    ) {
      connectWindow?.close();
      setConnectingProvider(null);
      setConnectWindow(null);
      toast.error("Gmail accounts must match your @gmail.com sign-in address.");
      return;
    }
    const timer = window.setInterval(() => {
      if (connectWindow?.closed) {
        setConnectingProvider(null);
        setConnectWindow(null);
        queryClient.invalidateQueries({ queryKey: ["integrations"] });
      }
    }, 500);
    return () => window.clearInterval(timer);
  }, [byProvider, connectWindow, connectingProvider, enforceGmailMatch, loginEmail, queryClient]);

  // The popup must open synchronously inside the click handler (before the
  // mutation's network round-trip) or the browser's popup blocker eats it.
  const startConnect = (provider: Provider) => {
    const popup = openNangoConnectWindow();
    if (!popup) {
      toast.error("Please allow popups to connect an integration.");
      return;
    }
    connect.mutate({ provider, popup });
  };

  const integrationsDisabled =
    connections.isError &&
    (connections.error as { response?: { status?: number } })?.response?.status === 503;

  const isBusy = connect.isPending || disconnect.isPending;
  const countConnected = (ids: Provider[]) => ids.filter((id) => byProvider.get(id)?.status === "connected").length;

  const tileProps = (id: Provider) => {
    const provider = providerDef(id);
    const connection = byProvider.get(id);
    const accountMismatch = Boolean(
      (provider.id === "gmail" || provider.id === "gmail_send") &&
        enforceGmailMatch &&
        connection?.account_email &&
        loginEmail &&
        connection.account_email.toLowerCase() !== loginEmail.toLowerCase(),
    );
    return {
      provider,
      connection,
      loading: connections.isLoading,
      busy: isBusy,
      awaitingPopup: connectingProvider === id,
      accountMismatch,
      loginEmail,
      onConnect: () => startConnect(id),
      onDisconnect: () => {
        if (window.confirm(`Disconnect ${provider.name}?`)) disconnect.mutate(id);
      },
    };
  };

  const heroAside = (
    <Bezel size="lg" coreClassName="p-6 md:p-7">
      <div aria-live="polite">
        <Stat
          label="Connected services"
          value={
            connections.isLoading ? (
              <span className="text-muted-foreground/60">—</span>
            ) : integrationsDisabled ? (
              <span className="text-muted-foreground/70">Off</span>
            ) : (
              <>
                {connectedCount}
                <span className="text-muted-foreground/50"> of {PROVIDERS.length}</span>
              </>
            )
          }
          hint={
            integrationsDisabled
              ? "Integrations are not enabled on this deployment."
              : `${connectedCount} of ${PROVIDERS.length} connected`
          }
        />
      </div>
      <ol aria-hidden className="mt-6 grid grid-cols-5 gap-1.5">
        {PROVIDERS.map((provider) => {
          const on = byProvider.get(provider.id)?.status === "connected";
          return (
            <li
              key={provider.id}
              title={provider.name}
              className={cn(
                "h-1.5 rounded-full transition-colors duration-700 ease-vanguard",
                on ? "bg-success" : "bg-foreground/[0.08] dark:bg-white/10",
              )}
            />
          );
        })}
      </ol>
      <div className="mt-4 flex items-center justify-between font-geist-mono text-[11px] tabular-nums text-muted-foreground">
        <span>Google {countConnected(GOOGLE_IDS)}/{GOOGLE_IDS.length}</span>
        <span>Microsoft {countConnected(MICROSOFT_IDS)}/{MICROSOFT_IDS.length}</span>
      </div>
    </Bezel>
  );

  return (
    <Screen>
      <PageHero
        eyebrow="Settings · Connected accounts"
        title="Integrations"
        accent="Your browser, inbox and calendar."
        description="Connect your browser for applications, and the services CareerCraft can use for approved email, calendar, and document actions."
        actions={
          <IslandLink
            href="#browser-extension"
            tone="ghost"
            size="md"
            trailing={<ArrowDown size={15} weight="light" />}
          >
            Set up browser applying
          </IslandLink>
        }
        aside={heroAside}
      />

      <Section aria-label="Integration settings" className="space-y-6 md:space-y-8">
        <Reveal subtle>
          <SettingsNav />
        </Reveal>

        <Reveal subtle>
          <GitHubSettings />
        </Reveal>

        {connections.isError && !integrationsDisabled ? (
          <Reveal subtle>
            <Notice tone="danger" icon={<WarningCircle size={16} weight="light" />}>
              Could not load integrations. Try again shortly.
            </Notice>
          </Reveal>
        ) : null}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <Reveal id="browser-extension" className="scroll-mt-24 lg:col-span-7 lg:row-span-2">
            <BrowserExtensionCard />
          </Reveal>

          {integrationsDisabled ? (
            <Reveal delay={0.08} className="lg:col-span-5 lg:row-span-2">
              <Bezel size="lg" tone="muted" className="h-full" coreClassName="flex h-full flex-col justify-center">
                <EmptyPanel
                  icon={<CloudSlash size={24} weight="light" />}
                  title="Account integrations are off"
                  description={
                    <>
                      {getApiErrorMessage(connections.error, "Email and calendar integrations are not enabled")} on this
                      deployment. Ask your administrator to configure Nango to connect Gmail, Outlook or Drive.
                    </>
                  }
                />
              </Bezel>
            </Reveal>
          ) : (
            <>
              <Reveal delay={0.08} className="lg:col-span-5">
                <ProviderGroup
                  eyebrow="Workspace"
                  title="Google"
                  icon={<GoogleLogo size={18} weight="light" />}
                  connected={countConnected(GOOGLE_IDS)}
                  total={GOOGLE_IDS.length}
                >
                  <ProviderTile variant="feature" {...tileProps("gmail")}>
                    {enforceGmailMatch ? (
                      <Notice tone="neutral" icon={<Info size={15} weight="light" />} className="py-3 text-xs leading-5">
                        Gmail must use the same <span className="font-medium text-foreground">{loginEmail}</span> address
                        as your CareerCraft sign-in.
                      </Notice>
                    ) : null}
                  </ProviderTile>
                  <ProviderTile variant="row" {...tileProps("gmail_send")} />
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <ProviderTile variant="compact" {...tileProps("google_drive")} />
                    <ProviderTile variant="compact" {...tileProps("google_calendar")} />
                  </div>
                </ProviderGroup>
              </Reveal>

              <Reveal delay={0.16} className="lg:col-span-5">
                <ProviderGroup
                  eyebrow="Microsoft 365"
                  title="Microsoft"
                  icon={<WindowsLogo size={18} weight="light" />}
                  connected={countConnected(MICROSOFT_IDS)}
                  total={MICROSOFT_IDS.length}
                >
                  <div className="space-y-3">
                    <ProviderTile variant="row" {...tileProps("outlook_mail")} />
                    <ProviderTile variant="row" {...tileProps("outlook_calendar")} />
                  </div>
                </ProviderGroup>
              </Reveal>
            </>
          )}
        </div>
      </Section>
    </Screen>
  );
}
