"use client";

import { AnimatePresence, motion } from "framer-motion";
import { ImageOff, Images } from "lucide-react";
import { useState } from "react";

import { mediaUrl } from "@/lib/api";
import { cn, formatClock } from "@/lib/utils";
import type { ScreenshotFrame } from "@/store";

/**
 * Live browser viewer: the newest frame large, with a clickable filmstrip of
 * history underneath. The screenshots are the visual half of the audit trail —
 * they are how you confirm the agent really was on the page it claims.
 */
export function ScreenshotViewer({ frames }: { frames: ScreenshotFrame[] }) {
  const [selected, setSelected] = useState<number | null>(null);
  const currentIndex = selected ?? frames.length - 1;
  const current = frames[currentIndex];

  return (
    <section className="card overflow-hidden" aria-label="Browser screenshots">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <Images className="h-3.5 w-3.5 text-faint" aria-hidden />
        <span className="text-[11px] font-medium text-muted">Browser</span>
        {current ? (
          <span className="truncate font-mono text-[10px] text-faint" title={current.url}>
            {current.url || current.label}
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[10px] text-faint">
          {frames.length} frame{frames.length === 1 ? "" : "s"}
        </span>
      </header>

      <div className="relative flex h-64 items-center justify-center bg-canvas sm:h-80">
        <AnimatePresence mode="wait">
          {current ? (
            <motion.img
              key={current.src + currentIndex}
              src={mediaUrl(current.src) ?? ""}
              alt={`Browser view at ${formatClock(current.at)}`}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="max-h-full max-w-full object-contain object-top"
            />
          ) : (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex flex-col items-center gap-2 text-center"
            >
              <ImageOff className="h-6 w-6 text-line2" aria-hidden />
              <p className="max-w-[16rem] text-xs text-faint">
                Screenshots appear here after each browser action.
              </p>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {frames.length > 0 ? (
        <div
          className="flex gap-1.5 overflow-x-auto border-t border-line bg-canvas px-2 py-2"
          role="tablist"
          aria-label="Screenshot history"
        >
          {frames.map((frame, index) => (
            <button
              key={`${frame.src}-${index}`}
              type="button"
              role="tab"
              aria-selected={index === currentIndex}
              aria-label={`Frame ${index + 1} at ${formatClock(frame.at)}`}
              onClick={() => setSelected(index)}
              className={cn(
                "relative h-11 w-16 shrink-0 overflow-hidden rounded-md border transition-colors",
                index === currentIndex ? "border-amber" : "border-line hover:border-line2",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mediaUrl(frame.src) ?? ""}
                alt=""
                className="h-full w-full object-cover object-top"
              />
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
