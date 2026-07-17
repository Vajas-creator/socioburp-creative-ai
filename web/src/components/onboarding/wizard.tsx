"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { ApiError, postJson, putJson } from "@/lib/api-client";
import {
  INDUSTRIES,
  MARKETING_GOALS,
  STEP_SCHEMAS,
  STEP_TITLES,
  TOTAL_STEPS,
  isOnboardingStep,
} from "@/lib/onboarding";
import { ErrorBanner, SuccessBanner } from "@/components/ui";

/** Flat, all-string form state mirroring the ClientOnboarding row. */
export interface OnboardingFormData {
  businessName: string;
  industry: string;
  businessDescription: string;
  website: string;
  businessAddress: string;
  timeZone: string;
  ownerName: string;
  contactEmail: string;
  phoneNumber: string;
  whatsappNumber: string;
  marketingGoals: string[];
  customGoal: string;
  facebookUrl: string;
  instagramUrl: string;
  linkedinUrl: string;
  youtubeUrl: string;
  googleBusinessUrl: string;
  twitterUrl: string;
  googleAdsAccountId: string;
  metaAdsAccountId: string;
  monthlyBudget: string;
  targetLocations: string[];
}

export interface WizardInitialState {
  data: OnboardingFormData;
  lastCompletedStep: number;
  completedAt: string | null;
}

type FieldErrors = Record<string, string[] | undefined>;

function buildStepPayload(step: number, form: OnboardingFormData): unknown {
  switch (step) {
    case 1:
      return {
        businessName: form.businessName,
        industry: form.industry,
        businessDescription: form.businessDescription,
        website: form.website,
        businessAddress: form.businessAddress,
        timeZone: form.timeZone,
      };
    case 2:
      return {
        ownerName: form.ownerName,
        contactEmail: form.contactEmail,
        phoneNumber: form.phoneNumber,
        whatsappNumber: form.whatsappNumber,
      };
    case 3:
      return {
        marketingGoals: form.marketingGoals,
        customGoal: form.customGoal,
      };
    case 4:
      return {
        facebookUrl: form.facebookUrl,
        instagramUrl: form.instagramUrl,
        linkedinUrl: form.linkedinUrl,
        youtubeUrl: form.youtubeUrl,
        googleBusinessUrl: form.googleBusinessUrl,
        twitterUrl: form.twitterUrl,
      };
    case 5:
      return {
        googleAdsAccountId: form.googleAdsAccountId,
        metaAdsAccountId: form.metaAdsAccountId,
        monthlyBudget: form.monthlyBudget,
        targetLocations: form.targetLocations,
      };
    default:
      return {};
  }
}

function getTimeZones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return ["UTC", "Asia/Kolkata", "Europe/London", "America/New_York"];
  }
}

export function OnboardingWizard({ initial }: { initial: WizardInitialState }) {
  const router = useRouter();
  const [form, setForm] = useState<OnboardingFormData>(initial.data);
  const [step, setStep] = useState(() =>
    initial.completedAt
      ? TOTAL_STEPS
      : Math.min(initial.lastCompletedStep + 1, TOTAL_STEPS)
  );
  const [maxReachedStep, setMaxReachedStep] = useState(() =>
    initial.completedAt ? TOTAL_STEPS : Math.min(initial.lastCompletedStep + 1, TOTAL_STEPS)
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const timeZones = useMemo(() => getTimeZones(), []);

  function update<K extends keyof OnboardingFormData>(
    key: K,
    value: OnboardingFormData[K]
  ) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function saveStep(target: number): Promise<boolean> {
    if (!isOnboardingStep(step)) return true;

    const payload = buildStepPayload(step, form);
    const clientCheck = STEP_SCHEMAS[step].safeParse(payload);
    if (!clientCheck.success) {
      setFieldErrors(clientCheck.error.flatten().fieldErrors as FieldErrors);
      setError("Please fix the highlighted fields.");
      return false;
    }

    setSaving(true);
    setError(null);
    setFieldErrors({});
    try {
      await putJson(`/api/onboarding/steps/${step}`, payload);
      setMaxReachedStep((m) => Math.max(m, target));
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.details) {
        setFieldErrors(err.details as FieldErrors);
      }
      setError(err instanceof Error ? err.message : "Could not save this step.");
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function goTo(target: number) {
    if (target === step) return;
    // Moving forward saves the current step first; moving back doesn't
    // discard anything since each step was saved when it was left.
    if (target > step) {
      const ok = await saveStep(target);
      if (!ok) return;
    } else {
      setError(null);
      setFieldErrors({});
    }
    setStep(target);
  }

  async function handleSubmit() {
    setSaving(true);
    setError(null);
    try {
      await postJson("/api/onboarding/submit", {});
      setSubmitted(true);
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError && err.incompleteSteps?.length) {
        setError(`${err.message} ${err.incompleteSteps.join("; ")}`);
      } else {
        setError(err instanceof Error ? err.message : "Could not submit.");
      }
    } finally {
      setSaving(false);
    }
  }

  if (submitted) {
    return (
      <div className="mx-auto max-w-2xl">
        <SuccessBanner message="Onboarding complete! Your business profile has been saved." />
        <div className="mt-4 flex gap-3">
          <Link
            href="/dashboard"
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500"
          >
            Back to dashboard
          </Link>
          <button
            type="button"
            onClick={() => setSubmitted(false)}
            className="rounded-lg border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Review answers
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl">
      <StepHeader current={step} maxReached={maxReachedStep} onSelect={goTo} />

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900 sm:p-8">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
          {STEP_TITLES[step]}
        </h2>

        <div className="mt-4">
          <ErrorBanner message={error} />

          {step === 1 && (
            <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
              <LabeledInput
                label="Business name *"
                value={form.businessName}
                onChange={(v) => update("businessName", v)}
                errors={fieldErrors.businessName}
                className="sm:col-span-2"
              />
              <LabeledSelect
                label="Industry *"
                value={form.industry}
                onChange={(v) => update("industry", v)}
                options={INDUSTRIES}
                placeholder="Select an industry"
                errors={fieldErrors.industry}
              />
              <LabeledSelect
                label="Time zone *"
                value={form.timeZone}
                onChange={(v) => update("timeZone", v)}
                options={timeZones}
                placeholder="Select a time zone"
                errors={fieldErrors.timeZone}
              />
              <LabeledTextarea
                label="Business description"
                value={form.businessDescription}
                onChange={(v) => update("businessDescription", v)}
                errors={fieldErrors.businessDescription}
                className="sm:col-span-2"
                rows={3}
              />
              <LabeledInput
                label="Website"
                value={form.website}
                onChange={(v) => update("website", v)}
                placeholder="https://example.com"
                errors={fieldErrors.website}
              />
              <LabeledInput
                label="Business address"
                value={form.businessAddress}
                onChange={(v) => update("businessAddress", v)}
                errors={fieldErrors.businessAddress}
              />
            </div>
          )}

          {step === 2 && (
            <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
              <LabeledInput
                label="Owner name *"
                value={form.ownerName}
                onChange={(v) => update("ownerName", v)}
                errors={fieldErrors.ownerName}
              />
              <LabeledInput
                label="Email *"
                type="email"
                value={form.contactEmail}
                onChange={(v) => update("contactEmail", v)}
                errors={fieldErrors.contactEmail}
              />
              <LabeledInput
                label="Phone number *"
                value={form.phoneNumber}
                onChange={(v) => update("phoneNumber", v)}
                placeholder="+91 98765 43210"
                errors={fieldErrors.phoneNumber}
              />
              <LabeledInput
                label="WhatsApp number"
                value={form.whatsappNumber}
                onChange={(v) => update("whatsappNumber", v)}
                placeholder="Same as phone if blank"
                errors={fieldErrors.whatsappNumber}
              />
            </div>
          )}

          {step === 3 && (
            <div>
              <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
                What should SocioBurp focus on? Pick all that apply.
              </p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {MARKETING_GOALS.map((goal) => (
                  <label
                    key={goal.id}
                    className="flex cursor-pointer items-center gap-3 rounded-lg border border-zinc-200 px-3 py-2.5 text-sm text-zinc-800 transition hover:border-indigo-400 has-[:checked]:border-indigo-500 has-[:checked]:bg-indigo-50 dark:border-zinc-700 dark:text-zinc-200 dark:has-[:checked]:bg-indigo-950"
                  >
                    <input
                      type="checkbox"
                      checked={form.marketingGoals.includes(goal.id)}
                      onChange={(e) =>
                        update(
                          "marketingGoals",
                          e.target.checked
                            ? [...form.marketingGoals, goal.id]
                            : form.marketingGoals.filter((g) => g !== goal.id)
                        )
                      }
                      className="h-4 w-4 accent-indigo-600"
                    />
                    {goal.label}
                  </label>
                ))}
              </div>
              <div className="mt-4">
                <LabeledInput
                  label="Custom goal"
                  value={form.customGoal}
                  onChange={(v) => update("customGoal", v)}
                  placeholder="Anything else you want to achieve?"
                  errors={fieldErrors.customGoal}
                />
              </div>
              {fieldErrors.marketingGoals && (
                <p className="mt-1 text-sm text-red-600 dark:text-red-400">
                  {fieldErrors.marketingGoals[0]}
                </p>
              )}
            </div>
          )}

          {step === 4 && (
            <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
              <LabeledInput label="Facebook" value={form.facebookUrl} onChange={(v) => update("facebookUrl", v)} placeholder="https://facebook.com/yourpage" errors={fieldErrors.facebookUrl} />
              <LabeledInput label="Instagram" value={form.instagramUrl} onChange={(v) => update("instagramUrl", v)} placeholder="https://instagram.com/yourhandle" errors={fieldErrors.instagramUrl} />
              <LabeledInput label="LinkedIn" value={form.linkedinUrl} onChange={(v) => update("linkedinUrl", v)} placeholder="https://linkedin.com/company/yours" errors={fieldErrors.linkedinUrl} />
              <LabeledInput label="YouTube" value={form.youtubeUrl} onChange={(v) => update("youtubeUrl", v)} placeholder="https://youtube.com/@yourchannel" errors={fieldErrors.youtubeUrl} />
              <LabeledInput label="Google Business Profile" value={form.googleBusinessUrl} onChange={(v) => update("googleBusinessUrl", v)} placeholder="https://g.page/yourbusiness" errors={fieldErrors.googleBusinessUrl} />
              <LabeledInput label="X (Twitter)" value={form.twitterUrl} onChange={(v) => update("twitterUrl", v)} placeholder="https://x.com/yourhandle" errors={fieldErrors.twitterUrl} />
            </div>
          )}

          {step === 5 && (
            <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
              <LabeledInput
                label="Google Ads account ID"
                value={form.googleAdsAccountId}
                onChange={(v) => update("googleAdsAccountId", v)}
                placeholder="123-456-7890"
                errors={fieldErrors.googleAdsAccountId}
              />
              <LabeledInput
                label="Meta Ads account ID"
                value={form.metaAdsAccountId}
                onChange={(v) => update("metaAdsAccountId", v)}
                placeholder="act_1234567890"
                errors={fieldErrors.metaAdsAccountId}
              />
              <LabeledInput
                label="Monthly marketing budget"
                type="number"
                value={form.monthlyBudget}
                onChange={(v) => update("monthlyBudget", v)}
                placeholder="e.g. 25000"
                errors={fieldErrors.monthlyBudget}
              />
              <LabeledTextarea
                label="Target locations (one per line)"
                value={form.targetLocations.join("\n")}
                onChange={(v) =>
                  update(
                    "targetLocations",
                    v.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
                  )
                }
                errors={fieldErrors.targetLocations}
                className="sm:col-span-2"
                rows={3}
              />
            </div>
          )}

          {step === 6 && <ReviewStep form={form} onEdit={goTo} />}
        </div>

        <div className="mt-6 flex items-center justify-between border-t border-zinc-100 pt-4 dark:border-zinc-800">
          <button
            type="button"
            onClick={() => goTo(step - 1)}
            disabled={saving || step === 1}
            className="rounded-lg border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 transition hover:bg-zinc-100 disabled:invisible dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Back
          </button>
          {step < TOTAL_STEPS ? (
            <button
              type="button"
              onClick={() => goTo(step + 1)}
              disabled={saving}
              className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-60"
            >
              {saving ? "Saving…" : "Save & continue"}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={saving}
              className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-60"
            >
              {saving ? "Submitting…" : initial.completedAt ? "Save changes" : "Finish onboarding"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function StepHeader({
  current,
  maxReached,
  onSelect,
}: {
  current: number;
  maxReached: number;
  onSelect: (step: number) => void;
}) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {Array.from({ length: TOTAL_STEPS }, (_, i) => i + 1).map((n) => {
        const reachable = n <= maxReached;
        const active = n === current;
        return (
          <li key={n} className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => reachable && onSelect(n)}
              disabled={!reachable}
              title={STEP_TITLES[n]}
              className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-semibold transition ${
                active
                  ? "bg-indigo-600 text-white"
                  : reachable
                    ? "bg-indigo-100 text-indigo-700 hover:bg-indigo-200 dark:bg-indigo-950 dark:text-indigo-300"
                    : "bg-zinc-100 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-600"
              }`}
            >
              {n}
            </button>
            <span
              className={`hidden text-xs sm:inline ${
                active
                  ? "font-semibold text-zinc-900 dark:text-zinc-100"
                  : "text-zinc-500 dark:text-zinc-400"
              }`}
            >
              {STEP_TITLES[n]}
            </span>
            {n < TOTAL_STEPS && (
              <span className="h-px w-3 bg-zinc-300 dark:bg-zinc-700 sm:w-5" />
            )}
          </li>
        );
      })}
    </ol>
  );
}

function ReviewStep({
  form,
  onEdit,
}: {
  form: OnboardingFormData;
  onEdit: (step: number) => void;
}) {
  const goalLabels = form.marketingGoals
    .map((id) => MARKETING_GOALS.find((g) => g.id === id)?.label ?? id)
    .concat(form.customGoal ? [`Custom: ${form.customGoal}`] : []);

  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Check everything below, edit any section, then finish to save your
        profile.
      </p>
      <ReviewSection title={STEP_TITLES[1]} step={1} onEdit={onEdit}>
        <ReviewRow label="Business name" value={form.businessName} />
        <ReviewRow label="Industry" value={form.industry} />
        <ReviewRow label="Description" value={form.businessDescription} />
        <ReviewRow label="Website" value={form.website} />
        <ReviewRow label="Address" value={form.businessAddress} />
        <ReviewRow label="Time zone" value={form.timeZone} />
      </ReviewSection>
      <ReviewSection title={STEP_TITLES[2]} step={2} onEdit={onEdit}>
        <ReviewRow label="Owner" value={form.ownerName} />
        <ReviewRow label="Email" value={form.contactEmail} />
        <ReviewRow label="Phone" value={form.phoneNumber} />
        <ReviewRow label="WhatsApp" value={form.whatsappNumber} />
      </ReviewSection>
      <ReviewSection title={STEP_TITLES[3]} step={3} onEdit={onEdit}>
        <ReviewRow label="Goals" value={goalLabels.join(", ")} />
      </ReviewSection>
      <ReviewSection title={STEP_TITLES[4]} step={4} onEdit={onEdit}>
        <ReviewRow label="Facebook" value={form.facebookUrl} />
        <ReviewRow label="Instagram" value={form.instagramUrl} />
        <ReviewRow label="LinkedIn" value={form.linkedinUrl} />
        <ReviewRow label="YouTube" value={form.youtubeUrl} />
        <ReviewRow label="Google Business" value={form.googleBusinessUrl} />
        <ReviewRow label="X (Twitter)" value={form.twitterUrl} />
      </ReviewSection>
      <ReviewSection title={STEP_TITLES[5]} step={5} onEdit={onEdit}>
        <ReviewRow label="Google Ads ID" value={form.googleAdsAccountId} />
        <ReviewRow label="Meta Ads ID" value={form.metaAdsAccountId} />
        <ReviewRow label="Monthly budget" value={form.monthlyBudget} />
        <ReviewRow label="Target locations" value={form.targetLocations.join(", ")} />
      </ReviewSection>
    </div>
  );
}

function ReviewSection({
  title,
  step,
  onEdit,
  children,
}: {
  title: string;
  step: number;
  onEdit: (step: number) => void;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {title}
        </h3>
        <button
          type="button"
          onClick={() => onEdit(step)}
          className="text-sm font-medium text-indigo-600 hover:text-indigo-500"
        >
          Edit
        </button>
      </div>
      <dl className="space-y-1">{children}</dl>
    </section>
  );
}

function ReviewRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 text-sm">
      <dt className="shrink-0 text-zinc-500 dark:text-zinc-400">{label}</dt>
      <dd className="truncate text-right font-medium text-zinc-900 dark:text-zinc-100">
        {value || <span className="font-normal text-zinc-400">—</span>}
      </dd>
    </div>
  );
}

const inputClasses =
  "block w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 placeholder-zinc-400 shadow-sm outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/30 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-50";

function FieldShell({
  label,
  errors,
  className = "",
  children,
}: {
  label: string;
  errors?: string[];
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`mb-4 ${className}`}>
      <label className="mb-1.5 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
        {label}
        {children}
      </label>
      {errors?.[0] && (
        <p className="mt-1 text-sm text-red-600 dark:text-red-400">{errors[0]}</p>
      )}
    </div>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  errors,
  className,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  errors?: string[];
  className?: string;
  type?: string;
  placeholder?: string;
}) {
  return (
    <FieldShell label={label} errors={errors} className={className}>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`mt-1.5 ${inputClasses}`}
      />
    </FieldShell>
  );
}

function LabeledTextarea({
  label,
  value,
  onChange,
  errors,
  className,
  rows = 3,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  errors?: string[];
  className?: string;
  rows?: number;
}) {
  return (
    <FieldShell label={label} errors={errors} className={className}>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        className={`mt-1.5 ${inputClasses}`}
      />
    </FieldShell>
  );
}

function LabeledSelect({
  label,
  value,
  onChange,
  options,
  placeholder,
  errors,
  className,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: readonly string[];
  placeholder: string;
  errors?: string[];
  className?: string;
}) {
  return (
    <FieldShell label={label} errors={errors} className={className}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`mt-1.5 ${inputClasses}`}
      >
        <option value="" disabled>
          {placeholder}
        </option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </FieldShell>
  );
}
