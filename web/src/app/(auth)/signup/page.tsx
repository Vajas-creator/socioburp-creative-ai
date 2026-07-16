"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AuthCard, ErrorBanner, Field, SubmitButton } from "@/components/ui";
import { postJson } from "@/lib/api-client";

export default function SignupPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    const form = new FormData(e.currentTarget);
    try {
      await postJson("/api/auth/signup", {
        name: form.get("name"),
        email: form.get("email"),
        password: form.get("password"),
      });
      router.push("/dashboard");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthCard
      title="Create your SocioBurp account"
      subtitle={
        <>
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-indigo-600 hover:text-indigo-500">
            Sign in
          </Link>
        </>
      }
    >
      <ErrorBanner message={error} />
      <form onSubmit={handleSubmit} noValidate>
        <Field label="Full name" id="name" type="text" autoComplete="name" required disabled={loading} />
        <Field
          label="Email"
          id="email"
          type="email"
          autoComplete="email"
          required
          disabled={loading}
        />
        <Field
          label="Password"
          id="password"
          type="password"
          autoComplete="new-password"
          required
          disabled={loading}
          minLength={8}
        />
        <p className="mb-4 text-xs text-zinc-500 dark:text-zinc-400">
          At least 8 characters, with an uppercase letter, a lowercase letter, a number, and a symbol.
        </p>
        <SubmitButton loading={loading}>Create account</SubmitButton>
      </form>
    </AuthCard>
  );
}
