import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/session";
import { getDashboardData } from "@/lib/dashboard-data";

export default async function AdminPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login?next=/dashboard/admin");
  if (user.role !== "ADMIN") redirect("/dashboard");

  const data = await getDashboardData(user);
  const recentUsers = data.role === "ADMIN" ? data.recentUsers : [];

  return (
    <div>
      <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">
        User management
      </h1>
      <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        Most recently created accounts.
      </p>

      <div className="mt-6 overflow-hidden rounded-2xl border border-zinc-200 dark:border-zinc-800">
        <table className="min-w-full divide-y divide-zinc-200 dark:divide-zinc-800">
          <thead className="bg-zinc-50 dark:bg-zinc-900">
            <tr>
              <Th>Name</Th>
              <Th>Email</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th>Joined</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 bg-white dark:divide-zinc-800 dark:bg-zinc-950">
            {recentUsers.map((u) => (
              <tr key={u.id}>
                <Td>{u.name}</Td>
                <Td>{u.email}</Td>
                <Td>{u.role}</Td>
                <Td>{u.isActive ? "Active" : "Disabled"}</Td>
                <Td>{new Date(u.createdAt).toLocaleDateString()}</Td>
              </tr>
            ))}
            {recentUsers.length === 0 && (
              <tr>
                <Td colSpan={5}>No users yet.</Td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
      {children}
    </th>
  );
}

function Td({
  children,
  colSpan,
}: {
  children: React.ReactNode;
  colSpan?: number;
}) {
  return (
    <td
      colSpan={colSpan}
      className="whitespace-nowrap px-4 py-3 text-sm text-zinc-700 dark:text-zinc-300"
    >
      {children}
    </td>
  );
}
