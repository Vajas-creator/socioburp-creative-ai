import { prisma } from "@/lib/prisma";
import type { AccessTokenPayload } from "@/lib/auth";

export async function getDashboardData(user: AccessTokenPayload) {
  if (user.role === "ADMIN") {
    const [totalUsers, byRole, recentUsers] = await Promise.all([
      prisma.user.count(),
      prisma.user.groupBy({ by: ["role"], _count: { role: true } }),
      prisma.user.findMany({
        orderBy: { createdAt: "desc" },
        take: 10,
        select: { id: true, name: true, email: true, role: true, createdAt: true, isActive: true },
      }),
    ]);

    return {
      role: "ADMIN" as const,
      stats: {
        totalUsers,
        byRole: Object.fromEntries(byRole.map((r) => [r.role, r._count.role])),
      },
      recentUsers,
    };
  }

  const profile = await prisma.user.findUnique({
    where: { id: user.sub },
    select: { id: true, name: true, email: true, role: true, createdAt: true },
  });

  return { role: user.role, profile };
}
