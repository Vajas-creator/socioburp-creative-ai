import Link from "next/link";
import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/session";
import { getDashboardData } from "@/lib/dashboard-data";
import { prisma } from "@/lib/prisma";

export default async function DashboardPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login?next=/dashboard");

  const [data, onboarding] = await Promise.all([
    getDashboardData(user),
    prisma.clientOnboarding.findUnique({
      where: { userId: user.sub },
      select: { lastCompletedStep: true, completedAt: true },
    }),
  ]);

  return (
    <div>
      <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">
        Welcome back, {user.name.split(" ")[0]}
      </h1>
      <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        You&apos;re signed in as {user.email}.
      </p>

      <div className="mt-6 flex flex-col gap-3 rounded-2xl border border-indigo-200 bg-indigo-50 p-5 dark:border-indigo-900 dark:bg-indigo-950 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-indigo-900 dark:text-indigo-200">
            Client onboarding
          </h2>
          <p className="mt-0.5 text-sm text-indigo-700 dark:text-indigo-300">
            {onboarding?.completedAt
              ? "Your business profile is complete. You can update it anytime."
              : onboarding
                ? `In progress — ${onboarding.lastCompletedStep} of 5 steps saved.`
                : "Set up your business profile so SocioBurp can work for you."}
          </p>
        </div>
        <Link
          href="/dashboard/onboarding"
          className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-center text-sm font-semibold text-white transition hover:bg-indigo-500"
        >
          {onboarding?.completedAt
            ? "Edit profile"
            : onboarding
              ? "Continue setup"
              : "Start onboarding"}
        </Link>
      </div>

      {data.role === "ADMIN" ? (
        <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard label="Total users" value={data.stats.totalUsers} />
          <StatCard label="Admins" value={data.stats.byRole.ADMIN ?? 0} />
          <StatCard label="Team members" value={data.stats.byRole.TEAM_MEMBER ?? 0} />
        </div>
      ) : (
        <div className="mt-8 rounded-2xl border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900">
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">
            Your profile
          </h2>
          <dl className="mt-4 space-y-2 text-sm">
            <Row label="Name" value={data.profile?.name ?? "—"} />
            <Row label="Email" value={data.profile?.email ?? "—"} />
            <Row label="Role" value={data.profile?.role ?? "—"} />
            <Row
              label="Member since"
              value={
                data.profile
                  ? new Date(data.profile.createdAt).toLocaleDateString()
                  : "—"
              }
            />
          </dl>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900">
      <p className="text-sm text-zinc-500 dark:text-zinc-400">{label}</p>
      <p className="mt-2 text-3xl font-semibold text-zinc-900 dark:text-zinc-50">
        {value}
      </p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-zinc-100 pb-2 last:border-0 dark:border-zinc-800">
      <dt className="text-zinc-500 dark:text-zinc-400">{label}</dt>
      <dd className="font-medium text-zinc-900 dark:text-zinc-100">{value}</dd>
    </div>
  );
}
