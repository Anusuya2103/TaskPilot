"use client";

import { AlertOctagon, RefreshCw } from "lucide-react";
import { useEffect } from "react";

/**
 * Route-level error boundary.
 *
 * A crashed page must never leave a blank screen: it says what broke, offers a
 * retry, and keeps the navigation available.
 */
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("TaskPilot UI error:", error);
  }, [error]);

  return (
    <div className="card mx-auto max-w-lg p-8 text-center">
      <AlertOctagon className="mx-auto h-7 w-7 text-danger" aria-hidden />
      <h1 className="mt-3 text-lg font-semibold">Something broke in the interface</h1>
      <p className="mt-1 text-sm text-muted">
        {error.message || "An unexpected rendering error occurred."}
      </p>
      {error.digest ? (
        <p className="mt-1 font-mono text-[10px] text-faint">digest {error.digest}</p>
      ) : null}
      <div className="mt-5 flex justify-center gap-2">
        <button type="button" onClick={reset} className="btn-primary">
          <RefreshCw className="h-4 w-4" aria-hidden /> Try again
        </button>
        <a href="/" className="btn-ghost">Back home</a>
      </div>
      <p className="mt-4 text-xs text-faint">
        The run itself is unaffected — its audit trail is still on the server.
      </p>
    </div>
  );
}
