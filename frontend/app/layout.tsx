import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";

import { Providers } from "@/components/providers";
import { SiteHeader } from "@/components/site-header";

import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "TaskPilot — Plan. Act. Pause. (For you.)",
    template: "%s · TaskPilot",
  },
  description:
    "An AI agent that takes a plain-language task, plans ordered steps, executes them across web apps, and pauses for your approval before anything risky happens.",
  openGraph: {
    title: "TaskPilot — Plan. Act. Pause. (For you.)",
    description:
      "Browser automation, MCP connectors and HTTP APIs with a human in the loop.",
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#0A0A0A",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable} dark`}>
      <body className="min-h-screen bg-canvas font-sans text-ink">
        <Providers>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50
                       focus:rounded-xl focus:bg-amber focus:px-4 focus:py-2 focus:text-sm
                       focus:font-semibold focus:text-canvas"
          >
            Skip to content
          </a>
          <SiteHeader />
          <main id="main" className="mx-auto w-full max-w-[1400px] px-4 pb-24 pt-6 sm:px-6">
            {children}
          </main>
          <footer className="border-t border-line py-6 text-center text-xs text-faint">
            TaskPilot · Team NeuroNova · Autonomous Agents for Everyday Apps
          </footer>
        </Providers>
      </body>
    </html>
  );
}
