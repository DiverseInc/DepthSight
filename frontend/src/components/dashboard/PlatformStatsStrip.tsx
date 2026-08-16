// src/components/dashboard/PlatformStatsStrip.tsx
//
// Compact horizontal strip showing platform-wide stats. Designed to make the
// dashboard feel "alive at scale" — for the investor demo and for any user
// landing on the dashboard. Data is public (no auth) so the strip works for
// non-logged-in visitors too.
//
// Endpoint: GET /api/v1/stats/platform (public, no auth)
// Refreshes every 60s. Shows skeleton placeholders during the first load so
// the layout doesn't shift when numbers arrive.

import { useEffect, useState } from "react";
import { Users, FileText, MessageCircle, Sparkles, BarChart3 } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

interface PlatformStats {
  totalUsers: number;
  strategyTemplates: number;
  communityNews: number;
  communityStrategyTopics: number;
  communityDiscussionTopics: number;
  totalBacktests: number;
}

const REFRESH_MS = 60_000;

function format(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(0)}K`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

export const PlatformStatsStrip = () => {
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const r = await fetch("/api/v1/stats/platform", { credentials: "include" });
        if (!r.ok) throw new Error(`status ${r.status}`);
        const json = await r.json();
        if (cancelled) return;
        setStats(json.data);
        setError(false);
      } catch {
        if (cancelled) return;
        setError(true);
        // Leave stats as null — the Skeleton placeholders will keep showing
        // rather than flashing an error to the user.
      }
    };

    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // While loading or on error, show skeleton placeholders. The card
  // itself always renders so the dashboard layout doesn't shift.
  const items: Array<{
    label: string;
    value: number | null;
    Icon: typeof Users;
    hue: string; // tailwind text color
  }> = [
    { label: "Traders",        value: stats?.totalUsers ?? null,            Icon: Users,         hue: "text-blue-500" },
    { label: "Templates",      value: stats?.strategyTemplates ?? null,    Icon: Sparkles,      hue: "text-amber-500" },
    { label: "Community posts",value: (stats?.communityStrategyTopics ?? 0) + (stats?.communityDiscussionTopics ?? 0) || null, Icon: MessageCircle, hue: "text-cyan-500" },
    { label: "Backtests run",  value: stats?.totalBacktests ?? null,       Icon: BarChart3,     hue: "text-emerald-500" },
    { label: "Announcements",  value: stats?.communityNews ?? null,        Icon: FileText,      hue: "text-violet-500" },
  ];

  return (
    <Card className="relative overflow-hidden border-primary/10 bg-gradient-to-r from-card/60 via-card/40 to-card/60">
      <CardContent className="px-4 py-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          {items.map(({ label, value, Icon, hue }, idx) => (
            <div
              key={label}
              className={`flex items-baseline gap-2 ${idx < items.length - 1 ? "pr-4 sm:border-r sm:border-border/40" : ""}`}
            >
              <Icon className={`w-3.5 h-3.5 ${hue} self-center`} />
              {value === null ? (
                <Skeleton className="h-5 w-10" />
              ) : (
                <span className="text-lg font-bold tabular-nums">
                  {format(value)}
                </span>
              )}
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground font-medium">
                {label}
              </span>
            </div>
          ))}
        </div>
        {error && !stats && (
          <p className="text-[10px] text-muted-foreground/60 mt-1 text-right">
            stats temporarily unavailable
          </p>
        )}
      </CardContent>
    </Card>
  );
};
