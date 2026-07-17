import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/session";
import { prisma } from "@/lib/prisma";
import {
  OnboardingWizard,
  type WizardInitialState,
} from "@/components/onboarding/wizard";
import type { ClientOnboarding } from "@/generated/prisma/client";

export const metadata = { title: "Client Onboarding — SocioBurp" };

function toInitialState(record: ClientOnboarding | null): WizardInitialState {
  const s = (v: string | null | undefined) => v ?? "";
  return {
    data: {
      businessName: s(record?.businessName),
      industry: s(record?.industry),
      businessDescription: s(record?.businessDescription),
      website: s(record?.website),
      businessAddress: s(record?.businessAddress),
      timeZone: s(record?.timeZone),
      ownerName: s(record?.ownerName),
      contactEmail: s(record?.contactEmail),
      phoneNumber: s(record?.phoneNumber),
      whatsappNumber: s(record?.whatsappNumber),
      marketingGoals: record?.marketingGoals ?? [],
      customGoal: s(record?.customGoal),
      facebookUrl: s(record?.facebookUrl),
      instagramUrl: s(record?.instagramUrl),
      linkedinUrl: s(record?.linkedinUrl),
      youtubeUrl: s(record?.youtubeUrl),
      googleBusinessUrl: s(record?.googleBusinessUrl),
      twitterUrl: s(record?.twitterUrl),
      googleAdsAccountId: s(record?.googleAdsAccountId),
      metaAdsAccountId: s(record?.metaAdsAccountId),
      monthlyBudget: record?.monthlyBudget != null ? String(record.monthlyBudget) : "",
      targetLocations: record?.targetLocations ?? [],
    },
    lastCompletedStep: record?.lastCompletedStep ?? 0,
    completedAt: record?.completedAt?.toISOString() ?? null,
  };
}

export default async function OnboardingPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login?next=/dashboard/onboarding");

  const record = await prisma.clientOnboarding.findUnique({
    where: { userId: user.sub },
  });

  return (
    <div>
      <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">
        Client onboarding
      </h1>
      <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        {record?.completedAt
          ? "Your profile is complete — you can update any section below."
          : "Tell us about your business so SocioBurp can work for you."}
      </p>
      <div className="mt-8">
        <OnboardingWizard initial={toInitialState(record)} />
      </div>
    </div>
  );
}
