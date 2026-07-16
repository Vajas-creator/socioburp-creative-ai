import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/session";
import { getDashboardData } from "@/lib/dashboard-data";

export default async function DashboardPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login?next=/dashboard");

  const data = await getDashboardData(user);

  return (
    <div>
      <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">
        Welcome back, {user.name.split(" ")[0]}
      </h1>
      <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        You&apos;re signed in as {user.email}.
      </p>

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
