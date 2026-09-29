"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, FlaskConical, History, Settings as SettingsIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Home", icon: FlaskConical, exact: true },
  { href: "/history", label: "History", icon: History },
  { href: "/settings", label: "Settings", icon: SettingsIcon },
];

export function SiteHeader() {
  const pathname = usePathname();
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
    staleTime: 60_000,
    retry: false,
  });

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-canvas/85 backdrop-blur-md">
      <div className="mx-auto flex h-14 w-full max-w-[1400px] items-center gap-4 px-4 sm:px-6">
        <Link
          href="/"
          className="group flex items-center gap-2 rounded-lg py-1 pr-2 text-[15px] font-semibold tracking-tight"
        >
          <span className="grid h-6 w-6 place-items-center rounded-md bg-amber text-[11px] font-black text-canvas">
            TP
          </span>
          <span>
            Task<span className="text-amber">Pilot</span>
          </span>
        </Link>

        <nav className="flex items-center gap-1" aria-label="Main">
          {NAV.map((item) => {
            const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors",
                  active ? "bg-line text-ink" : "text-muted hover:bg-line/60 hover:text-ink",
                )}
              >
                <Icon className="h-3.5 w-3.5" aria-hidden />
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          {config?.demo_mode ? (
            <span
              className="pill border-amber/40 bg-amber/10 text-amber"
              title="Running on the deterministic scripted brain — no API key configured"
            >
              <Activity className="h-3 w-3" aria-hidden />
              Demo mode
            </span>
          ) : null}
          <span className="hidden font-mono text-[11px] text-faint sm:inline">
            {config?.version ?? ""}
          </span>
        </div>
      </div>
    </header>
  );
}
