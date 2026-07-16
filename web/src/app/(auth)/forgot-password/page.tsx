"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { AuthCard, ErrorBanner, Field, SubmitButton, SuccessBanner } from "@/components/ui";
import { postJson } from "@/lib/api-client";

export default function ForgotPasswordPage() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setMessage(null);
    setLoading(true);

    const form = new FormData(e.currentTarget);
    try {
      const data = await postJson<{ message: string }>("/api/auth/forgot-password", {
        email: form.get("email"),
      });
      setMessage(data.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthCard
      title="Reset your password"
      subtitle={
        <>
          Remembered it?{" "}
          <Link href="/login" className="font-medium text-indigo-600 hover:text-indigo-500">
            Back to sign in
          </Link>
        </>
      }
    >
      <ErrorBanner message={error} />
      <SuccessBanner message={message} />
      {!message && (
        <form onSubmit={handleSubmit} noValidate>
          <Field
            label="Email"
            id="email"
            type="email"
            autoComplete="email"
            required
            disabled={loading}
          />
          <SubmitButton loading={loading}>Send reset link</SubmitButton>
        </form>
      )}
    </AuthCard>
  );
}
