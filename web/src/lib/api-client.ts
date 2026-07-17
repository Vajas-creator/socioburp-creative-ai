/** Thin fetch wrapper for calling our own API routes from client components. */
async function requestJson<T>(
  method: "POST" | "PUT",
  path: string,
  body: unknown
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });

  const data = await res.json().catch(() => ({}));

  if (!res.ok) {
    const message =
      typeof data?.error === "string" ? data.error : "Something went wrong.";
    throw new ApiError(message, data?.details, data?.incompleteSteps);
  }

  return data as T;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public details?: Record<string, string[]>,
    public incompleteSteps?: string[]
  ) {
    super(message);
  }
}

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return requestJson<T>("POST", path, body);
}

export function putJson<T>(path: string, body: unknown): Promise<T> {
  return requestJson<T>("PUT", path, body);
}
