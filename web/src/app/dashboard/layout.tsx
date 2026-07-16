import Link from "next/link";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { getCurrentUser } from "@/lib/session";
import { LogoutButton } from "@/components/logout-button";

const ROLE_LABELS: Record<string, string> = {
  ADMIN: "Admin",
  TEAM_MEMBER: "Team Member",
  CLIENT: "Client",
};

export default async function DashboardLayout({
  children,
}: {
  children: ReactNode;
}) {
  // Defense in depth: src/proxy.ts already gates /dashboard/**, but every
  // Server Component re-verifies independently rather than trusting Proxy.
  const user = await getCurrentUser();
  if (!user) {
    redirect("/login?next=/dashboard");
  }

  return (
    <div className="flex min-h-screen flex-1 flex-col bg-zinc-50 dark:bg-black">
      <header className="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-6">
            <span className="rounded-lg bg-indigo-600 px-2.5 py-1 text-sm font-semibold text-white">
              SocioBurp
            </span>
            <nav className="flex items-center gap-4 text-sm font-medium text-zinc-600 dark:text-zinc-400">
              <Link href="/dashboard" className="hover:text-zinc-900 dark:hover:text-zinc-100">
                Dashboard
              </Link>
              {user.role === "ADMIN" && (
                <Link
                  href="/dashboard/admin"
                  className="hover:text-zinc-900 dark:hover:text-zinc-100"
                >
                  Admin
                </Link>
              )}
            </nav>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-right">
              <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                {user.name}
              </p>
              <p className="text-xs text-zinc-500 dark:text-zinc-400">
                {ROLE_LABELS[user.role] ?? user.role}
              </p>
            </div>
            <LogoutButton />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-8">{children}</main>
    </div>
  );
}
