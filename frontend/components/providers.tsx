import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { ToastHost, useToastStore } from "@/components/toast";
import { useRunStore } from "@/store";

/**
 * App providers: React Query for server state, a small Zustand store for the
 * things that must survive navigation (the live event log, toasts).
 */
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Live runs are polled only as a backstop; SSE is the primary channel.
            staleTime: 2000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              const status = (error as { status?: number })?.status;
              // Never retry client errors; do retry transient server issues.
              if (typeof status === "number" && status >= 400 && status < 500) return false;
              return failureCount < 2;
            },
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <StoreBridge />
      {children}
      <ToastHost />
    </QueryClientProvider>
  );
}

/** Subscribe the toast store to API errors raised anywhere in the tree. */
function StoreBridge() {
  const push = useToastStore((s: { push: (t: { kind: "info" | "success" | "warning" | "error"; title: string; body?: string }) => void }) => s.push);

  useEffect(() => {
    const unsubscribe = useRunStore.subscribe((state, previous) => {
      if (state.lastError && state.lastError !== previous.lastError) {
        push({ kind: "error", title: state.lastError });
      }
    });
    return unsubscribe;
  }, [push]);

  return null;
}
