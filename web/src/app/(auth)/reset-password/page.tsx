"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { AuthCard, ErrorBanner, Field, SubmitButton, SuccessBanner } from "@/components/ui";
import { postJson } from "@/lib/api-client";

function ResetPasswordForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    const form = new FormData(e.currentTarget);
    const password = form.get("password") as string;
    const confirmPassword = form.get("confirmPassword") as string;

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      setLoading(false);
      return;
    }

    try {
      const data = await postJson<{ message: string }>("/api/auth/reset-password", {
        token,
        password,
      });
      setMessage(data.message);
      setTimeout(() => router.push("/login"), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  if (!token) {
    return (
      <AuthCard title="Reset your password">
        <ErrorBanner message="This reset link is missing its token. Request a new one from the forgot password page." />
        <Link href="/forgot-password" className="text-sm font-medium text-indigo-600 hover:text-indigo-500">
          Request a new link
        </Link>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Choose a new password">
      <ErrorBanner message={error} />
      <SuccessBanner message={message} />
      {!message && (
        <form onSubmit={handleSubmit} noValidate>
          <Field
            label="New password"
            id="password"
            type="password"
            autoComplete="new-password"
            required
            disabled={loading}
            minLength={8}
          />
          <Field
            label="Confirm new password"
            id="confirmPassword"
            type="password"
            autoComplete="new-password"
            required
            disabled={loading}
            minLength={8}
          />
          <SubmitButton loading={loading}>Reset password</SubmitButton>
        </form>
      )}
    </AuthCard>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetPasswordForm />
    </Suspense>
  );
}
