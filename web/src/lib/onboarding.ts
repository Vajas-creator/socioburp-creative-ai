import { z } from "zod";

/**
 * Shared definitions for the 6-step client onboarding wizard: the goal
 * catalog, per-step zod schemas (enforced server-side in the API routes,
 * reused client-side for inline errors), and the step metadata the wizard
 * UI renders from.
 */

export const MARKETING_GOALS = [
  { id: "generate_leads", label: "Generate Leads" },
  { id: "increase_sales", label: "Increase Sales" },
  { id: "brand_awareness", label: "Brand Awareness" },
  { id: "website_traffic", label: "Website Traffic" },
  { id: "appointments", label: "Appointments" },
] as const;

export type MarketingGoalId = (typeof MARKETING_GOALS)[number]["id"];

const GOAL_IDS = MARKETING_GOALS.map((g) => g.id) as [
  MarketingGoalId,
  ...MarketingGoalId[],
];

export const INDUSTRIES = [
  "Restaurant / Food",
  "Salon / Beauty",
  "Retail / E-commerce",
  "Health / Fitness",
  "Real Estate",
  "Education",
  "Professional Services",
  "Travel / Hospitality",
  "Technology",
  "Other",
] as const;

// Empty strings from cleared form fields become null so they're stored as
// SQL NULL rather than "".
const optionalTrimmed = (max: number) =>
  z
    .string()
    .trim()
    .max(max)
    .transform((v) => (v === "" ? null : v))
    .nullish();

const optionalUrl = z
  .string()
  .trim()
  .max(300)
  .transform((v) => (v === "" ? null : v))
  .nullish()
  .refine(
    (v) => v == null || /^https?:\/\/[^\s]+\.[^\s]+/.test(v),
    "Must be a full URL starting with http:// or https://"
  );

const optionalPhone = z
  .string()
  .trim()
  .max(20)
  .transform((v) => (v === "" ? null : v))
  .nullish()
  .refine(
    (v) => v == null || /^\+?[0-9][0-9\s\-()]{6,18}$/.test(v),
    "Must be a valid phone number (digits, +, spaces, dashes)"
  );

export const businessInfoSchema = z.object({
  businessName: z.string().trim().min(1, "Business name is required").max(200),
  industry: z.string().trim().min(1, "Industry is required").max(100),
  businessDescription: optionalTrimmed(2000),
  website: optionalUrl,
  businessAddress: optionalTrimmed(500),
  timeZone: z
    .string()
    .trim()
    .min(1, "Time zone is required")
    .max(100)
    .refine((tz) => {
      try {
        new Intl.DateTimeFormat("en", { timeZone: tz });
        return true;
      } catch {
        return false;
      }
    }, "Must be a valid IANA time zone (e.g. Asia/Kolkata)"),
});

export const contactInfoSchema = z.object({
  ownerName: z.string().trim().min(1, "Owner name is required").max(200),
  contactEmail: z.string().trim().toLowerCase().email("Invalid email address"),
  phoneNumber: z
    .string()
    .trim()
    .min(7, "Phone number is required")
    .max(20)
    .regex(
      /^\+?[0-9][0-9\s\-()]{6,18}$/,
      "Must be a valid phone number (digits, +, spaces, dashes)"
    ),
  whatsappNumber: optionalPhone,
});

export const marketingGoalsSchema = z
  .object({
    marketingGoals: z.array(z.enum(GOAL_IDS)).max(GOAL_IDS.length),
    customGoal: optionalTrimmed(300),
  })
  .refine(
    (data) => data.marketingGoals.length > 0 || data.customGoal != null,
    { message: "Select at least one goal or describe a custom goal", path: ["marketingGoals"] }
  );

export const socialAccountsSchema = z.object({
  facebookUrl: optionalUrl,
  instagramUrl: optionalUrl,
  linkedinUrl: optionalUrl,
  youtubeUrl: optionalUrl,
  googleBusinessUrl: optionalUrl,
  twitterUrl: optionalUrl,
});

export const advertisingSchema = z.object({
  googleAdsAccountId: optionalTrimmed(50).refine(
    (v) => v == null || /^[0-9]{3}-?[0-9]{3}-?[0-9]{4}$/.test(v),
    "Google Ads account IDs look like 123-456-7890"
  ),
  metaAdsAccountId: optionalTrimmed(50).refine(
    (v) => v == null || /^(act_)?[0-9]{5,20}$/.test(v),
    "Meta Ads account IDs are numeric (optionally prefixed act_)"
  ),
  monthlyBudget: z
    .union([z.number(), z.string().trim()])
    .transform((v) => (v === "" || v == null ? null : Number(v)))
    .nullish()
    .refine(
      (v) => v == null || (Number.isInteger(v) && v >= 0 && v <= 100_000_000),
      "Budget must be a whole number of your currency units"
    ),
  targetLocations: z
    .array(z.string().trim().min(1).max(200))
    .max(50, "Too many locations")
    .default([]),
});

export const STEP_SCHEMAS = {
  1: businessInfoSchema,
  2: contactInfoSchema,
  3: marketingGoalsSchema,
  4: socialAccountsSchema,
  5: advertisingSchema,
} as const;

export type OnboardingStep = keyof typeof STEP_SCHEMAS;

export const STEP_TITLES: Record<number, string> = {
  1: "Business Information",
  2: "Contact Information",
  3: "Marketing Goals",
  4: "Social Accounts",
  5: "Advertising",
  6: "Review & Finish",
};

export const TOTAL_STEPS = 6;

/** Steps whose schemas contain required fields — final submit re-checks these. */
export const REQUIRED_STEPS: OnboardingStep[] = [1, 2, 3];

export function isOnboardingStep(value: number): value is OnboardingStep {
  return Number.isInteger(value) && value >= 1 && value <= 5;
}
