"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { motion } from "motion/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  CheckCircle,
  CircleNotch,
  Cpu,
  Eye,
  EyeSlash,
  HardDrives,
  Key,
  Lightning,
  LockSimple,
  Moon,
  Sun,
  Trash,
  XCircle,
} from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { SettingsNav } from "@/components/settings/SettingsNav";
import { useTheme, type Theme } from "@/components/theme/ThemeProvider";
import {
  Bezel,
  EmptyPanel,
  Field,
  Hairline,
  IconButton,
  Input,
  IslandButton,
  PageHero,
  PanelTitle,
  Reveal,
  Screen,
  Segmented,
  Select,
  Skeleton,
  StatusPill,
  listItem,
  listStagger,
} from "@/components/vanguard";
import { cn } from "@/lib/utils";
import { PROVIDERS, PROVIDER_MODELS, type Provider } from "@/lib/model-providers";
import { PrivacyCard } from "./PrivacyCard";

interface ModelSetting {
  id: string;
  provider: string;
  model_name: string | null;
  is_active: boolean;
}

/**
 * One numbered step of the add-provider form: an editorial label column on
 * the left (md+) and the control on the right. Stacks on mobile.
 */
function FormStep({
  index,
  title,
  hint,
  children,
}: {
  index: string;
  title: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 py-6 first:pt-0 last:pb-0 md:grid-cols-[10rem_minmax(0,1fr)] md:gap-8">
      <div className="min-w-0">
        <p className="font-geist-mono text-[11px] tabular-nums tracking-[0.08em] text-muted-foreground/80">{index}</p>
        <p className="mt-1.5 text-sm font-medium tracking-[-0.01em] text-foreground">{title}</p>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{hint}</p>
      </div>
      <div className="min-w-0 space-y-4">{children}</div>
    </div>
  );
}

export default function SettingsModelsPage() {
  const { theme, toggleTheme } = useTheme();
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [modelName, setModelName] = useState<string>(PROVIDER_MODELS.anthropic[0]);
  const [customMode, setCustomMode] = useState(false);
  const [ollamaUrl, setOllamaUrl] = useState("http://localhost:11434");
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, "ok" | "fail">>({});

  const { data: models = [], isLoading: modelsLoading } = useQuery<ModelSetting[]>({
    queryKey: ["models"],
    queryFn: () => apiClient.get("/users/me/models").then((r) => r.data as ModelSetting[]),
  });

  const { mutate: addModel, isPending } = useMutation({
    mutationFn: () =>
      apiClient.post("/users/me/models", {
        provider,
        api_key: apiKey,
        model_name: modelName,
        ollama_url: provider === "ollama" ? ollamaUrl : null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      setApiKey("");
      toast.success("Model added");
    },
    onError: (err: unknown) => {
      const data = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      const msg = Array.isArray(data)
        ? (data[0] as { msg?: string })?.msg ?? "Invalid input"
        : typeof data === "string"
          ? data
          : "Failed to add model";
      toast.error(msg);
    },
  });

  const { mutate: activateModel } = useMutation({
    mutationFn: (id: string) => apiClient.patch(`/users/me/models/${id}/activate`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      toast.success("Model activated");
    },
    onError: () => toast.error("Failed to activate model"),
  });

  const { mutate: deleteModel } = useMutation({
    mutationFn: (id: string) => apiClient.delete(`/users/me/models/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      toast.success("Model removed");
    },
    onError: () => toast.error("Failed to remove model"),
  });

  const testModel = async (modelId: string) => {
    setTestingId(modelId);
    try {
      const { data } = await apiClient.post("/users/me/models/test", { model_id: modelId });
      setTestResults((r) => ({ ...r, [modelId]: "ok" }));
      toast.success(`Model working: ${(data as { response: string }).response}`);
    } catch (err: unknown) {
      setTestResults((r) => ({ ...r, [modelId]: "fail" }));
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Test failed";
      toast.error(detail);
    } finally {
      setTestingId(null);
    }
  };

  const onProviderChange = (next: Provider) => {
    setProvider(next);
    setCustomMode(false);
    setModelName(PROVIDER_MODELS[next][0] ?? "");
  };

  const isOllama = provider === "ollama";
  const saveDisabled = isPending || (!apiKey && !isOllama);
  const activeModel = models.find((m) => m.is_active);

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (saveDisabled) return;
    addModel();
  };

  return (
    <Screen>
      <div className="space-y-6 md:space-y-8">
        <PageHero
          className="pb-4 md:pb-6"
          eyebrow="AI Models"
          title="Model Settings"
          accent="Your keys, your routing."
          description="Bring your own model keys, test provider health, and keep routing explicit. One active model powers every agent."
          actions={
            <div role="status" aria-live="polite">
              {modelsLoading ? (
                <StatusPill tone="neutral" live>
                  Checking configured models…
                </StatusPill>
              ) : activeModel ? (
                <StatusPill tone="success">
                  Routing to <span className="font-geist-mono">{activeModel.provider}</span>
                  {activeModel.model_name ? (
                    <span className="font-geist-mono text-success/80">/ {activeModel.model_name}</span>
                  ) : null}
                </StatusPill>
              ) : (
                <StatusPill tone="warning">No model selected — agent runs are paused</StatusPill>
              )}
            </div>
          }
        />

        <Reveal subtle>
          <SettingsNav />
        </Reveal>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12 lg:gap-8">
          {/* ── Left: add-provider form ─────────────────────────────── */}
          <Reveal className="min-w-0 lg:col-span-7">
            <Bezel lifted coreClassName="p-6 md:p-8">
              <form id="add-provider" onSubmit={onSubmit} aria-labelledby="add-provider-title" noValidate>
                <div className="flex flex-col gap-2 pb-6">
                  <PanelTitle
                    icon={<Key size={16} weight="light" />}
                    title={<span id="add-provider-title">Add provider</span>}
                    meta={<span className="font-geist-mono tabular-nums">{PROVIDERS.length} providers</span>}
                  />
                  <p className="max-w-[56ch] text-sm leading-6 text-muted-foreground">
                    Choose a provider and model. Keys are encrypted at rest and never shown again after saving.
                  </p>
                </div>

                <Hairline />

                <div className="divide-y divide-foreground/[0.06] pt-6 dark:divide-white/[0.07]">
                  <FormStep index="01" title="Provider" hint="Where requests are sent.">
                    <Field label="Provider">
                      {(id) => (
                        <Select
                          id={id}
                          name="provider"
                          value={provider}
                          onChange={(e) => onProviderChange(e.target.value as Provider)}
                        >
                          {PROVIDERS.map((p) => (
                            <option key={p.value} value={p.value}>
                              {p.label}
                            </option>
                          ))}
                        </Select>
                      )}
                    </Field>
                  </FormStep>

                  <FormStep index="02" title="Model" hint="Pick from the catalog or enter any ID your key supports.">
                    <Field label="Model">
                      {(id) => (
                        <Select
                          id={id}
                          name="model_name"
                          className="font-geist-mono text-[13px]"
                          value={customMode ? "__custom__" : modelName}
                          onChange={(e) => {
                            const v = e.target.value;
                            if (v === "__custom__") {
                              setCustomMode(true);
                              setModelName("");
                            } else {
                              setCustomMode(false);
                              setModelName(v);
                            }
                          }}
                        >
                          {PROVIDER_MODELS[provider].map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                          <option value="__custom__">Custom…</option>
                        </Select>
                      )}
                    </Field>
                    {customMode && (
                      <Field label="Custom model ID">
                        {(id) => (
                          <Input
                            id={id}
                            name="custom_model_name"
                            className="font-geist-mono text-[13px]"
                            value={modelName}
                            onChange={(e) => setModelName(e.target.value)}
                            placeholder="Type a model ID your key supports"
                            autoFocus
                          />
                        )}
                      </Field>
                    )}
                  </FormStep>

                  <FormStep
                    index="03"
                    title={isOllama ? "Endpoint" : "Credentials"}
                    hint={isOllama ? "Local models run without a key." : "Paste the key from your provider console."}
                  >
                    <Field
                      label="API key"
                      hint={!isPending && apiKey ? "Key encrypted before storage." : undefined}
                    >
                      {(id) => (
                        <Input
                          id={id}
                          name="api_key"
                          type={showApiKey ? "text" : "password"}
                          value={apiKey}
                          onChange={(e) => setApiKey(e.target.value)}
                          placeholder={isOllama ? "No API key required" : "Paste provider API key"}
                          disabled={isOllama}
                          autoComplete="new-password"
                          spellCheck={false}
                          className="font-geist-mono text-[13px]"
                          leading={<LockSimple size={15} weight="light" />}
                          trailing={
                            !isOllama ? (
                              <IconButton
                                size="sm"
                                aria-label={showApiKey ? "Hide API key" : "Show API key"}
                                aria-controls={id}
                                aria-pressed={showApiKey}
                                onClick={() => setShowApiKey((visible) => !visible)}
                              >
                                {showApiKey ? <EyeSlash size={15} weight="light" /> : <Eye size={15} weight="light" />}
                              </IconButton>
                            ) : undefined
                          }
                        />
                      )}
                    </Field>

                    {isOllama && (
                      <Field label="Ollama URL" hint="The server must be reachable from the CareerCraft backend.">
                        {(id) => (
                          <Input
                            id={id}
                            name="ollama_url"
                            value={ollamaUrl}
                            onChange={(e) => setOllamaUrl(e.target.value)}
                            placeholder="http://localhost:11434"
                            className="font-geist-mono text-[13px]"
                            leading={<HardDrives size={15} weight="light" />}
                          />
                        )}
                      </Field>
                    )}
                  </FormStep>
                </div>

                <div className="mt-8 flex flex-col-reverse gap-4 rounded-2xl bg-foreground/[0.025] p-4 ring-1 ring-foreground/[0.05] dark:bg-white/[0.025] dark:ring-white/[0.07] sm:flex-row sm:items-center sm:justify-between">
                  <p className="flex items-center gap-2 text-xs leading-5 text-muted-foreground">
                    <LockSimple aria-hidden size={14} weight="light" className="shrink-0" />
                    Saving makes this the active model for all agents.
                  </p>
                  <IslandButton
                    type="submit"
                    tone="primary"
                    size="md"
                    disabled={saveDisabled}
                    aria-busy={isPending}
                    trailing={
                      isPending ? (
                        <CircleNotch size={15} weight="light" className="animate-spin motion-reduce:animate-none" />
                      ) : (
                        <Key size={15} weight="light" />
                      )
                    }
                  >
                    {isPending ? "Saving…" : "Save API key"}
                  </IslandButton>
                </div>
              </form>
            </Bezel>
          </Reveal>

          {/* ── Right: configured models + theme ───────────────────── */}
          <div className="min-w-0 space-y-6 lg:col-span-5">
            <Reveal delay={0.08}>
              <section aria-labelledby="configured-models-title" className="space-y-4">
                <div className="flex items-end justify-between gap-4 px-1">
                  <div className="min-w-0">
                    <h2
                      id="configured-models-title"
                      className="font-geist text-xl font-semibold tracking-[-0.03em] text-foreground md:text-2xl"
                    >
                      Configured models
                    </h2>
                    <p className="mt-1 text-sm leading-6 text-muted-foreground">
                      One active model used by all agents. Test before activating.
                    </p>
                  </div>
                  {!modelsLoading && models.length > 0 ? (
                    <span className="shrink-0 rounded-full bg-foreground/[0.04] px-2.5 py-1 font-geist-mono text-[11px] tabular-nums text-muted-foreground ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10">
                      {String(models.length).padStart(2, "0")}
                    </span>
                  ) : null}
                </div>

                {modelsLoading ? (
                  <div className="space-y-3" aria-hidden>
                    {[0, 1].map((i) => (
                      <Bezel key={i} size="sm" coreClassName="rounded-2xl p-4">
                        <Skeleton className="h-4 w-32 rounded-full" />
                        <Skeleton className="mt-2 h-3 w-48 rounded-full" />
                        <Skeleton className="mt-4 h-9 w-full rounded-full" />
                      </Bezel>
                    ))}
                  </div>
                ) : models.length === 0 ? (
                  <Bezel tone="muted">
                    <EmptyPanel
                      compact
                      icon={<Cpu size={22} weight="light" />}
                      title="No models yet"
                      description="Add one to enable agent runs."
                    />
                  </Bezel>
                ) : (
                  <motion.ul
                    initial="hidden"
                    animate="show"
                    variants={listStagger}
                    className="space-y-3"
                    aria-label="Configured models"
                  >
                    {models.map((m) => {
                      const testResult = testResults[m.id];
                      const isTesting = testingId === m.id;
                      return (
                        <motion.li key={m.id} variants={listItem}>
                          <Bezel
                            size="sm"
                            tone={m.is_active ? "primary" : "default"}
                            coreClassName="rounded-2xl p-4"
                          >
                            <div data-testid="model-row">
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex min-w-0 items-center gap-3">
                                  <span
                                    aria-hidden
                                    className={cn(
                                      "grid h-9 w-9 shrink-0 place-items-center rounded-full ring-1",
                                      m.is_active
                                        ? "bg-primary/10 text-primary ring-primary/20"
                                        : "bg-foreground/[0.04] text-foreground/70 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10",
                                    )}
                                  >
                                    <Cpu size={16} weight="light" />
                                  </span>
                                  <div className="min-w-0">
                                    <div
                                      data-testid="model-provider"
                                      className="text-sm font-medium truncate tracking-[-0.01em] text-foreground"
                                    >
                                      {m.provider}
                                    </div>
                                    <div
                                      data-testid="model-name"
                                      className="text-xs text-muted-foreground truncate font-geist-mono"
                                    >
                                      {m.model_name ?? "—"}
                                    </div>
                                  </div>
                                </div>

                                <div className="flex shrink-0 items-center gap-2" aria-live="polite">
                                  {testResult === "ok" && (
                                    <span className="grid place-items-center text-success">
                                      <CheckCircle aria-hidden size={18} weight="light" />
                                      <span className="sr-only">Last test passed</span>
                                    </span>
                                  )}
                                  {testResult === "fail" && (
                                    <span className="grid place-items-center text-danger">
                                      <XCircle aria-hidden size={18} weight="light" />
                                      <span className="sr-only">Last test failed</span>
                                    </span>
                                  )}
                                  {m.is_active && <StatusPill tone="success">Active</StatusPill>}
                                </div>
                              </div>

                              <div className="mt-4 flex flex-wrap items-center gap-2">
                                <IslandButton
                                  tone="ghost"
                                  size="sm"
                                  onClick={() => testModel(m.id)}
                                  disabled={isTesting}
                                  aria-busy={isTesting}
                                  icon={
                                    isTesting ? (
                                      <CircleNotch size={14} weight="light" className="animate-spin motion-reduce:animate-none" />
                                    ) : (
                                      <Lightning size={14} weight="light" />
                                    )
                                  }
                                >
                                  {isTesting ? "Testing…" : "Test"}
                                </IslandButton>

                                {!m.is_active && (
                                  <IslandButton
                                    tone="quiet"
                                    size="sm"
                                    className="text-primary ring-primary/25 hover:bg-primary/10 hover:text-primary dark:hover:bg-primary/10"
                                    onClick={() => activateModel(m.id)}
                                  >
                                    Set active
                                  </IslandButton>
                                )}

                                <IslandButton
                                  tone="quiet"
                                  size="sm"
                                  className="ml-auto hover:bg-danger/10 hover:text-danger dark:hover:bg-danger/10"
                                  icon={<Trash size={14} weight="light" />}
                                  onClick={() => {
                                    if (confirm(`Remove ${m.provider} / ${m.model_name ?? "model"}?`)) {
                                      deleteModel(m.id);
                                    }
                                  }}
                                >
                                  Delete
                                </IslandButton>
                              </div>
                            </div>
                          </Bezel>
                        </motion.li>
                      );
                    })}
                  </motion.ul>
                )}
              </section>
            </Reveal>

            <Reveal delay={0.12}>
              <PrivacyCard />
            </Reveal>

            <Reveal delay={0.14}>
              <Bezel tone="muted" coreClassName="p-6">
                <PanelTitle
                  icon={theme === "dark" ? <Moon size={16} weight="light" /> : <Sun size={16} weight="light" />}
                  title="Theme"
                />
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  Current: <span className="capitalize text-foreground">{theme}</span>. Saved in your browser.
                </p>
                <Segmented<Theme>
                  className="mt-5"
                  asTabs={false}
                  ariaLabel="Colour theme"
                  value={theme}
                  onChange={(next) => {
                    if (next !== theme) toggleTheme();
                  }}
                  options={[
                    { value: "light", label: "Light", icon: <Sun size={14} weight="light" /> },
                    { value: "dark", label: "Dark", icon: <Moon size={14} weight="light" /> },
                  ]}
                />
              </Bezel>
            </Reveal>
          </div>
        </div>
      </div>
    </Screen>
  );
}
