/** Shared backend base URL, used by the live call page and the session
 * review UI. Never a secret: this is a local dev URL, not a credential. */
export const API_URL =
  process.env.NEXT_PUBLIC_TURNTRACE_API_URL ?? "http://localhost:7860";
